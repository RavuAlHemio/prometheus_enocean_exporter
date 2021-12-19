#!/usr/bin/env python3
import argparse
from collections import defaultdict
import logging
import queue
from threading import Lock
import time
from typing import Any, DefaultDict, Dict, Iterable, NamedTuple, Optional, Tuple
from enocean.communicators.serialcommunicator import SerialCommunicator
from enocean.protocol.constants import RORG
from prometheus_client import Metric, REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily
import yaml


class MetaValueAtTime(NamedTuple):
    value: Any
    mono_timestamp: float
    unix_timestamp: float


class ValueAtTime(NamedTuple):
    meta: MetaValueAtTime
    raw_value: float
    description: str
    unit: str


LOGGER = logging.getLogger('prometheus_enocean_exporter')
DEFAULT_MAX_VALUE_AGE_S = 15.0 * 60.0


def obtain_mono_timestamp() -> float:
    return time.monotonic()


def obtain_unix_timestamp() -> float:
    return time.time()


class EnOceanCollector:
    def __init__(self) -> None:
        self.serial_port: Optional[str] = None
        self.known_device_file: Optional[str] = None
        self.learning: bool = True
        self.max_value_age_s: float = DEFAULT_MAX_VALUE_AGE_S

        self._data_lock = Lock()

        # SenderID -> (RorgFunc, RorgType)
        self.known_4bs_eeps: Dict[str, Tuple[int, int]] = {}

        # SenderID -> ValueShortcut -> value
        self.values: DefaultDict[str, Dict[str, ValueAtTime]] = defaultdict(dict)

        # SenderID -> dBm
        self.sender_dbm: Dict[str, MetaValueAtTime] = {}


    def collect(self) -> Iterable[Metric]:
        value_metric = GaugeMetricFamily(
            'enocean_value',
            'A value obtained from an EnOcean device.',
            labels=['source', 'key'],
        )
        raw_value_metric = GaugeMetricFamily(
            'enocean_value_raw',
            'A raw value obtained from an EnOcean device.',
            labels=['source', 'key'],
        )
        value_description_metric = GaugeMetricFamily(
            'enocean_value_description',
            'The description for a value obtained from an EnOcean device.',
            labels=['source', 'key', 'description'],
        )
        value_unit_metric = GaugeMetricFamily(
            'enocean_value_unit',
            'The unit for a value obtained from an EnOcean device.',
            labels=['source', 'key', 'unit'],
        )
        value_timestamp_metric = GaugeMetricFamily(
            'enocean_value_timestamp',
            (
                'The Unix timestamp of the last update of a value obtained from'
                ' an EnOcean device.'
            ),
            labels=['source', 'key'],
        )
        dbm_metric = GaugeMetricFamily(
            'enocean_last_transmission_dBm',
            (
                'The strength of the last transmission of an EnOcean device'
                ' in decibels referenced to one milliwatt (dBm).'
            ),
            labels=['source'],
        )
        transmission_timestamp_metric = GaugeMetricFamily(
            'enocean_last_transmission_timestamp',
            (
                'The Unix timestamp of the last transmission of an EnOcean'
                ' device.'
            ),
            labels=['source'],
        )

        with self._data_lock:
            mono_now = obtain_mono_timestamp()
            for sender, shortcut_value in self.values.items():
                for shortcut, value in shortcut_value.items():
                    if mono_now - value.meta.mono_timestamp > self.max_value_age_s:
                        # value has aged out
                        continue
                    value_timestamp_metric.add_metric(
                        [sender, shortcut], value.meta.unix_timestamp,
                    )

                    if isinstance(value.meta.value, (int, float)):
                        value_metric.add_metric(
                            [sender, shortcut], value.meta.value,
                        )
                    raw_value_metric.add_metric(
                        [sender, shortcut], value.raw_value,
                    )
                    value_description_metric.add_metric(
                        [sender, shortcut, value.description], 1,
                    )
                    value_unit_metric.add_metric(
                        [sender, shortcut, value.unit], 1,
                    )
            for sender, dbm_at_time in self.sender_dbm.items():
                if mono_now - dbm_at_time.mono_timestamp > self.max_value_age_s:
                    # value has aged out
                    continue
                dbm_metric.add_metric(
                    [sender], dbm_at_time.value
                )
                transmission_timestamp_metric.add_metric(
                    [sender], dbm_at_time.unix_timestamp,
                )

        yield value_metric
        yield raw_value_metric
        yield value_description_metric
        yield value_unit_metric
        yield value_timestamp_metric
        yield dbm_metric
        yield transmission_timestamp_metric


    def _load_known_devices(self) -> None:
        if self.known_device_file is None:
            return

        # load known-device file
        try:
            with open(self.known_device_file, 'r', encoding='utf-8') as f:
                known_devices = yaml.safe_load(f)
                self.known_4bs_eeps.update(
                    known_devices.get('known_4bs_eeps', {}),
                )
        except FileNotFoundError:
            pass


    def _write_known_devices(self) -> None:
        if self.known_device_file is None:
            return

        known_devices = {
            'known_4bs_eeps': self.known_4bs_eeps,
        }
        with open(self.known_device_file, 'w', encoding='utf-8') as f:
            yaml.safe_dump(known_devices, f)


    def run(self) -> None:
        self._load_known_devices()

        cereal = SerialCommunicator(port=self.serial_port)
        cereal.start()

        # pre-obtain the base ID
        base_id = cereal.base_id
        LOGGER.info("communicator base ID: %s", base_id)

        try:
            LOGGER.info("ready to read")
            while cereal.is_alive():
                try:
                    packet = cereal.receive.get(block=True, timeout=1)
                except queue.Empty:
                    continue

                LOGGER.debug('obtained packet: %s', packet)

                sender_hex = getattr(packet, 'sender_hex', None)
                if sender_hex is None:
                    LOGGER.debug('no sender_hex; skipping packet')
                    continue

                dbm = getattr(packet, 'dBm', None)
                if dbm is not None:
                    with self._data_lock:
                        self.sender_dbm[sender_hex] = MetaValueAtTime(
                            value=dbm,
                            mono_timestamp=obtain_mono_timestamp(),
                            unix_timestamp=obtain_unix_timestamp(),
                        )

                if packet.learn and packet.rorg != RORG.VLD:
                    # new device to learn
                    if not self.learning:
                        # but we don't want to
                        LOGGER.debug("learning packet but we are not learning")
                        continue

                    if not getattr(packet, 'contains_eep', False):
                        # TODO: handle packets without EEP
                        LOGGER.debug("learning packet without EEP")
                        continue

                    # know this device
                    with self._data_lock:
                        self.known_4bs_eeps[packet.sender_hex] = (
                            packet.rorg_func, packet.rorg_type
                        )

                    LOGGER.info(
                        "learned that sender %s has ROrg func 0x%02x type 0x%02x",
                        packet.sender_hex, packet.rorg_func, packet.rorg_type
                    )

                    # update known-device file
                    self._write_known_devices()

                    continue

                # non-learning packet
                rorg_func_type = self.known_4bs_eeps.get(packet.sender_hex)
                if rorg_func_type is None:
                    # unknown source
                    LOGGER.debug("source %s is not known", packet.sender_hex)
                    continue
                rorg_func, rorg_type = rorg_func_type

                packet.parse_eep(rorg_func, rorg_type)
                LOGGER.debug("parsed packet: %r", packet.parsed)

                # add timestamps
                shortcut_to_value: Dict[str, ValueAtTime] = {}
                for short, val in packet.parsed.items():
                    shortcut_to_value[short] = ValueAtTime(
                        meta=MetaValueAtTime(
                            value=val['value'],
                            mono_timestamp=obtain_mono_timestamp(),
                            unix_timestamp=obtain_unix_timestamp(),
                        ),
                        raw_value=val['raw_value'],
                        description=val['description'],
                        unit=val['unit'],
                    )

                with self._data_lock:
                    self.values[packet.sender_hex].update(shortcut_to_value)
                    LOGGER.debug(
                        "values for %s updated to %r",
                        packet.sender_hex, self.values[packet.sender_hex],
                    )

        finally:
            cereal.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--web.listen-port",
        dest='web_listen_port', type=int, required=True,
    )
    parser.add_argument(
        "--web.listen-address",
        dest='web_listen_address', type=str, default="",
    )
    parser.add_argument(
        "--serial.port",
        dest='serial_port', type=str, default=None,
    )
    parser.add_argument(
        "--config.known-device-file",
        dest='known_device_file', type=str, default=None,
    )
    parser.add_argument(
        "--config.max-value-age",
        dest='max_value_age', metavar='SECONDS', type=float,
        default=DEFAULT_MAX_VALUE_AGE_S
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger('enocean').setLevel(logging.WARNING)

    if args.serial_port == '':
        args.serial_port = None
    if args.known_device_file == '':
        args.known_device_file = None

    enocean_collector = EnOceanCollector()
    enocean_collector.serial_port = args.serial_port
    enocean_collector.known_device_file = args.known_device_file
    enocean_collector.max_value_age_s = args.max_value_age
    REGISTRY.register(enocean_collector)

    start_http_server(args.web_listen_port, args.web_listen_address)

    enocean_collector.run()


if __name__ == '__main__':
    main()
