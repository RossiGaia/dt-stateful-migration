from flask import Flask, jsonify
from enum import Enum
import random
import time
import threading
import json
import os
import paho.mqtt.client as mqtt
import logging
import signal

# Global vars
# Application
app = Flask(__name__)
no_sensors = int(os.environ.get("NO_SENSORS", 100))

# logging
logger = logging.getLogger(__name__)
log_server = logging.getLogger('werkzeug')
log_server.setLevel(logging.ERROR)
logging.basicConfig(level=logging.WARNING)

# MQTT
mqtt_broker = os.environ.get("MQTT_BROKER")
mqtt_port = os.environ.get("MQTT_PORT")
mqtt_topic = os.environ.get("MQTT_TOPIC")
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
if mqtt_broker is None or mqtt_port is None or mqtt_topic is None:
    logger.error("Required vars for MQTT connection are not correctly configured.")
    exit(1)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info(f"Connected to MQTT Broker at {mqtt_broker}")


mqtt_client.on_connect = on_connect
mqtt_client.connect(mqtt_broker, int(mqtt_port))
mqtt_client.loop_start()

def graceful_shutdown(signum, frame):
    global rotating_machine

    logger.info("Shutting down.")
    rotating_machine.stop_simulation()
    exit(0)


# Signals handling
signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


class SensorState(Enum):
    WORKING = 0
    STOPPED = 1


class Sensor:
    def __init__(
        self,
        name="sensor",
        sample_rate=1,
        measuring_unit="[s]",
        min_value=0.0,
        reading_range=100.0,
    ):
        self._name = name
        self._state = SensorState.STOPPED
        self._reading = None
        self._measuring_unit = measuring_unit
        self._sample_rate = sample_rate

        self._lock = threading.Lock()
        self._min_reading = min_value
        self._reading_range = reading_range
        self._running_simulation = False
        self._sensor_thread = None

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

    @property
    def running_simulation(self):
        with self._lock:
            return self._running_simulation

    @running_simulation.setter
    def running_simulation(self, value):
        with self._lock:
            self._running_simulation = value

    def _read(self):
        if self.state != SensorState.WORKING:
            return None

        trend = time.time() % 10
        noise = random.gauss(0, 0.5)
        random_reading = self._min_reading + (self._reading_range / 2) + trend + noise
        self.value = random_reading

    def sensor_thread(self):
        while self.running_simulation:
            self._read()
            time.sleep(1 / self.sampling_rate)

    def run_sensor_simulation(self):
        if not self.running_simulation:
            self.running_simulation = True
            self.state = SensorState.WORKING
            self._sensor_thread = threading.Thread(
                target=self.sensor_thread, daemon=True
            )
            self._sensor_thread.start()

    def stop_sensor_simulation(self):
        self.running_simulation = False
        self.state = SensorState.STOPPED

    def to_json(self):
        return {
            "name": self.name,
            "state": self.state.name,
            "value": self.value,
            "measuring_unit": self.measuring_unit,
            "sampling_rate": self.sampling_rate,
        }


class RotatingMachine:
    def __init__(self, name="rotating_machine", sensors_list=[]):
        self._name = name
        self._sensors = {sensor.name: sensor for sensor in sensors_list}

        self._lock = threading.Lock()
        self._running_simulation = False
        self._simulation_thread = None

    @property
    def name(self):
        return self._name

    @property
    def sensors(self):
        return self._sensors

    @property
    def running_simulation(self):
        with self._lock:
            return self._running_simulation

    @running_simulation.setter
    def running_simulation(self, value):
        with self._lock:
            self._running_simulation = value

    def start_simulation(self):
        for sensor in self.sensors.values():
            sensor.run_sensor_simulation()

        self.running_simulation = True
        self._simulation_thread = threading.Thread(
            target=self.publish_to_mqtt_thread, daemon=True
        )
        self._simulation_thread.start()

    def stop_simulation(self):
        global mqtt_client
        for sensor in self.sensors.values():
            sensor.stop_sensor_simulation()

        self.running_simulation = False

        if self._simulation_thread:
            self._simulation_thread.join(timeout=5)

        mqtt_client.loop_stop()
        mqtt_client.disconnect()

    def publish_to_mqtt_thread(self):
        global mqtt_client
        while self.running_simulation:
            readings = []
            for sensor in self.sensors.values():
                read = {
                    "sensor": sensor.name,
                    "value": sensor.value,
                    "timestamp": time.time(),
                }
                readings.append(read)

            mqtt_client.publish(
                f"{mqtt_topic}/{self.name}",
                json.dumps({"readings": readings, "timestamp": time.time()}),
            )
            logger.info(f"Message size in MB: {len(json.dumps(readings)) / 1024 / 1024}")
            logger.debug(f"Published message:")
            for read in readings:
                logger.debug(f"{read["sensor"]}: {read["value"]}")
            time.sleep(1)

    def to_json(self):
        return {
            "name": self.name,
            "sensors": [sensor.to_json() for sensor in self.sensors.values()],
        }

@app.route("/sensor/<string:sensor_name>", methods=["GET"])
def get_sensor(sensor_name):
    if sensor_name == '':
        return {"message": "sensor_name parameter is not set."}, 400
    
    try:
        sensor = rotating_machine.sensors[sensor_name]
    except KeyError:
        return {"message": "sensor_name not valid."}, 404
    
    return jsonify({f"{sensor_name}": sensor.to_json()})

@app.route("/sensors", methods=["GET"])
def get_sensors():
    sensor_data = [
        {"sensor": sensor.name, "state": sensor.state.name, "value": sensor.value}
        for sensor in rotating_machine.sensors.values()
    ]

    return jsonify(sensor_data)


@app.route("/machine", methods=["GET"])
def get_machine():
    return jsonify(rotating_machine.to_json())


if __name__ == "__main__":

    sensors_list = [Sensor(f"sensor_{i}") for i in range(no_sensors)]
    rotating_machine = RotatingMachine("rotating_machine_1", sensors_list)

    rotating_machine.start_simulation()
    app.run(host="0.0.0.0", port=8000)
