from flask import Flask, request
from enum import Enum
import signal
import time
import threading
import json
import os
import paho.mqtt.client as mqtt
import logging
import collections
import requests

# Global vars
# logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# MQTT
mqtt_broker = os.environ.get("MQTT_BROKER")
mqtt_port = os.environ.get("MQTT_PORT")
mqtt_topic = os.environ.get("MQTT_TOPIC")
if mqtt_broker is None or mqtt_port is None or mqtt_topic is None:
    logger.error("Required vars for MQTT connection are not correctly configured.")
    exit(1)


# ODTE
odte_threshold = float(os.environ.get("ODTE_THRESHOLD", 0.6))

# Application
app = Flask(__name__)
average_threshold = float(os.environ.get("AVERAGE_THRESHOLD", 55.0))
observations_deque_lenght = int(os.environ.get("OBSERVATIONS_DEQUE_LENGHT", 100))
messages_deque_lenght = int(os.environ.get("MESSAGES_DEQUE_LENGHT", 100))
no_sensors = int(os.environ.get("NO_SENSORS", 100))
physical_twin_name = "rotating_machine_1"
migrated = bool(os.environ.get("MIGRATED", False))

# Measurements
exec_measurements = collections.deque(maxlen=messages_deque_lenght)
exec_measurements_file_path = os.environ.get(
    "EXEC_MEASUREMENTS_FILE_PATH", "/var/log/dt/exec_measurements.txt"
)

def graceful_shutdown(signum, frame):
    global digital_twin, exec_measurements, exec_measurements_file_path
    exit_code = 0
    logger.info("Shutting down.")

    try:
        with open(exec_measurements_file_path, "w+") as file:
            json.dump(list(exec_measurements), file)
    except Exception as e:
        logger.error(f"Error while writing exec_measurements.txt. {e}")
        logger.warning(f"Printing exec times on console: {list(exec_measurements)}")
        exit_code = 1
    finally:
        digital_twin.disconnect_from_mqtt()
        exit(exit_code)


# Signals handling
signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


class VirtualSensorState(Enum):
    WORKING = 0
    STOPPED = 1


class VirtualSensor:
    def __init__(
        self,
        name="sensor",
        sample_rate=1,
        measuring_unit="[s]",
        state=VirtualSensorState.STOPPED,
        reading=None,
    ):
        self._name = name
        self._state = state
        self._reading = reading
        self._measuring_unit = measuring_unit
        self._sample_rate = sample_rate

        self._lock = threading.Lock()

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        with self._lock:
            return self._state

    @state.setter
    def state(self, state):
        with self._lock:
            self._state = state

    @property
    def value(self):
        with self._lock:
            return self._reading

    @value.setter
    def value(self, value):
        with self._lock:
            self._reading = value

    @property
    def measuring_unit(self):
        return self._measuring_unit

    @property
    def sampling_rate(self):
        return self._sample_rate

    def to_json(self):
        return {
            "name": self.name,
            "state": self.state.name,
            "value": self.value,
            "measuring_unit": self.measuring_unit,
            "sampling_rate": self.sampling_rate,
        }


class VirtualRotatingMachine:
    def __init__(self, name="rotating_machine", sensors_list=[]):
        self._name = name
        self._sensors = {sensor.name: sensor for sensor in sensors_list}

    @property
    def name(self):
        return self._name

    @property
    def sensors(self):
        return self._sensors

    def to_json(self):
        return {
            "name": self.name,
            "sensors": [sensor.to_json() for sensor in self.sensors.values()],
        }


class DigitalTwinState(Enum):
    UNBOUND = 0
    BOUND = 1
    ENTANGLED = 2
    DISENTANGLED = 3
    DONE = 4


class DigitalTwin:
    def __init__(self):
        global mqtt_broker, mqtt_port, mqtt_topic, physical_twin_name, observations_deque_lenght, messages_deque_lenght
        self._state = DigitalTwinState.UNBOUND
        self._object = VirtualRotatingMachine(
            physical_twin_name,
            [VirtualSensor(f"sensor_{i}") for i in range(no_sensors)],
        )
        self._odte = None
        self._messages = collections.deque(maxlen=messages_deque_lenght)
        self._observations = collections.deque(maxlen=observations_deque_lenght)
        self._average = 0.0

        self._lock = threading.Lock()
        self._sums = collections.deque(maxlen=messages_deque_lenght)

        odte_t = threading.Thread(target=self.odte_thread, daemon=True)
        odte_t.start()

        if not migrated:
            self.connect_to_mqtt_and_subscribe(mqtt_broker, int(mqtt_port), mqtt_topic)

    @property
    def state(self):
        with self._lock:
            return self._state

    @state.setter
    def state(self, value):
        with self._lock:
            self._state = value

    @property
    def obj(self):
        with self._lock:
            return self._object

    @obj.setter
    def obj(self, value):
        with self._lock:
            self._object = value

    @property
    def odte(self):
        with self._lock:
            return self._odte

    @odte.setter
    def odte(self, value):
        with self._lock:
            self._odte = value

    @property
    def messages_deque(self):
        with self._lock:
            return self._messages

    @messages_deque.setter
    def messages_deque(self, value):
        with self._lock:
            self._messages = collections.deque(value, maxlen=messages_deque_lenght)

    @property
    def observations(self):
        with self._lock:
            return self._observations

    @observations.setter
    def observations(self, value):
        with self._lock:
            self._observations = collections.deque(
                value, maxlen=observations_deque_lenght
            )

    @property
    def average(self):
        with self._lock:
            return self._average

    @average.setter
    def average(self, average):
        with self._lock:
            self._average = average

    @property
    def sums(self):
        with self._lock:
            return self._sums

    @sums.setter
    def sums(self, value):
        with self._lock:
            self._sums = collections.deque(value, maxlen=messages_deque_lenght)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info(f"Connected to MQTT Broker at {mqtt_broker}")

    def on_message(self, client, userdata, message):
        global exec_measurements

        on_message_exec_start = time.time()

        received_timestamp = time.time()
        start_exec_time = time.time()

        data = json.loads(message.payload)
        self.messages_deque.append(data)

        for read in data["readings"]:
            sensor_to_update = self.obj.sensors[read["sensor"]]
            sensor_to_update.value = read["value"]
            self._sums.append(sensor_to_update.value)

        if len(self._sums) > 0:
            self.average = sum(self._sums) / len(self._sums)
        else:
            self.average = 0.0
        logger.info(f"Current average: {self.average}.")

        if self.average > average_threshold:
            logger.warning(f"Average over threshold: {self.average}.")

        end_exec_time = time.time()
        execution_timestamp = end_exec_time - start_exec_time
        message_timestamp = data["timestamp"]

        # odte timeliness computation
        self.observations.append(
            received_timestamp - message_timestamp + execution_timestamp
        )

        for sensor in self.obj.sensors.values():
            logger.debug(f"{sensor.name}: {sensor.value}")

        on_message_exec_total = time.time() - on_message_exec_start
        exec_measurements.append(on_message_exec_total)

    def connect_to_mqtt_and_subscribe(self, broker_ip, broker_port, topic):
        self._MQTT_CLIENT = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._MQTT_CLIENT.on_connect = self.on_connect
        self._MQTT_CLIENT.on_message = self.on_message

        self._MQTT_CLIENT.connect(broker_ip, broker_port)
        self._MQTT_CLIENT.subscribe(f"{topic}/{self.obj.name}")

        self.state = DigitalTwinState.BOUND

        self._MQTT_CLIENT.loop_start()

    def disconnect_from_mqtt(self):
        self._MQTT_CLIENT.loop_stop()
        self.state = DigitalTwinState.UNBOUND

    def compute_timeliness(self, desired_timeliness_sec: float) -> float:
        obs_list = list(self.observations)

        if len(obs_list) == 0:
            return 0.0

        count = 0
        for obs in obs_list:
            if obs <= desired_timeliness_sec:
                count += 1

        percentile = float(count / len(obs_list))

        return percentile

    def compute_reliability(
        self, window_length_sec: int, expected_msg_sec: int
    ) -> float:
        end_window_time = time.time()
        start_window_time = time.time() - window_length_sec

        msg_list = list(self.messages_deque)
        msg_required = msg_list[-window_length_sec * expected_msg_sec :]

        count = 0
        for msg in msg_required:
            if (
                msg["timestamp"] >= start_window_time
                and msg["timestamp"] <= end_window_time
            ):
                count += 1

        expected_msg_tot = window_length_sec * expected_msg_sec

        return float(count / expected_msg_tot)

    def compute_availability(self) -> float:
        return 1.0

    def compute_odte_phytodig(
        self, window_length_sec, desired_timeliness_sec, expected_msg_sec
    ):
        timeliness = self.compute_timeliness(desired_timeliness_sec)
        reliability = self.compute_reliability(window_length_sec, expected_msg_sec)
        availability = self.compute_availability()

        logger.debug(
            f"Availability: {availability}\tReliability: {reliability}\tTimeliness: {timeliness}"
        )

        return timeliness * reliability * availability

    def odte_thread(self):
        global odte_threshold
        while True:
            logger.debug(f"Computing odte {time.time()}")
            if (
                self.state == DigitalTwinState.BOUND
                or self.state == DigitalTwinState.ENTANGLED
                or self.state == DigitalTwinState.DISENTANGLED
            ):
                computed_odte = self.compute_odte_phytodig(10, 0.5, 1)
                self.odte = computed_odte
                logger.info(f"ODTE computed: {computed_odte}, state: {self.state}")
                if (
                    computed_odte < odte_threshold
                    and self.state == DigitalTwinState.ENTANGLED
                ):
                    self.state = DigitalTwinState.DISENTANGLED
                if computed_odte > odte_threshold and (
                    self.state == DigitalTwinState.DISENTANGLED
                    or self.state == DigitalTwinState.BOUND
                ):
                    self.state = DigitalTwinState.ENTANGLED
            time.sleep(1)

    def requery(self, url, iterations_to_rebuild = 100, seconds_between_requests = 1):
        for i in range(iterations_to_rebuild):
            logger.debug(f"Iteration no: {i}")
            try:
                resp = requests.get(url)
                data = json.loads(resp.text)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.ConnectTimeout,
            ):
                return False
            
            for sensor in data:
                try:
                    getattr(self.obj.sensors, sensor["sensor"])
                    self.obj.sensors[sensor["sensor"]].name = sensor["sensor"]
                    self.obj.sensors[sensor["sensor"]].state = VirtualSensorState[sensor["state"]]
                    self.obj.sensors[sensor["sensor"]].value = sensor["value"]
                except AttributeError:
                    continue
            time.sleep(seconds_between_requests)

        self.connect_to_mqtt_and_subscribe(mqtt_broker, int(mqtt_port), mqtt_topic)


# {"url": "<url>"}
@app.route("/requery", methods=["POST"])
def requery():
    global digital_twin
    data = request.get_json()
    pt_url = data["url"]

    digital_twin.requery(pt_url)
    return {"message": "requeried"}, 201


@app.route("/metrics")
def odte_prometheus():
    global digital_twin
    prometheus_template = (
        f'odte[pt="{digital_twin.obj.name}"] {str(digital_twin.odte)}'.replace(
            "[", "{"
        ).replace("]", "}")
    )
    return prometheus_template


if __name__ == "__main__":
    digital_twin = DigitalTwin()
    app.run(host="0.0.0.0", port=8001)
