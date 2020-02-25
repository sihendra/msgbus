import json
import multiprocessing
import os
import signal

import pika
from pika.channel import Channel
from pika.spec import Basic, BasicProperties

from msgbuzz import MessageBus, ConsumerConfirm, Message


class RabbitMqMessageBus(MessageBus):

    def __init__(self, host='localhost'):
        self._subscribers = {}
        self._conn_params = pika.ConnectionParameters(host=host)
        self._conn = pika.BlockingConnection(self._conn_params)
        self._consumers = []

    def publish(self, topic_name, body, headers=None):
        channel = self._conn.channel()
        message = Message(headers, body)
        msg_json = json.dumps(message.__dict__)
        channel.basic_publish(exchange=topic_name, routing_key='', body=msg_json,
                              properties=BasicProperties(content_type="application/json"))

    def on(self, topic_name, client_group, callback):
        self._subscribers[topic_name] = (client_group, callback)

    def start_consuming(self):
        signal.signal(signal.SIGINT, self._signal_handler)

        for topic_name, (client_group, callback) in self._subscribers.items():
            consumer = RabbitMqConsumer(self._conn_params, topic_name, client_group, callback)
            self._consumers.append(consumer)
            consumer.start()

        for consumer in self._consumers:
            consumer.join()

    def _signal_handler(self, sig, frame):
        print(f"You pressed Ctrl+C!")
        for consumer in self._consumers:
            consumer.stop()

        print("Stopping consumers")

        # sys.exit(0)


class RabbitMqConsumer(multiprocessing.Process):

    def __init__(self, conn_params, topic_name, client_group, callback):
        super().__init__()
        self._conn_params = conn_params
        self._topic_name = topic_name
        self._client_group = client_group
        self._callback = _callback_wrapper(callback)
        self._is_interrupted = False

    def stop(self):
        self._is_interrupted = True

    def run(self):
        # create new conn
        # rabbitmq best practice 1 process 1 conn, 1 thread 1 channel
        conn = pika.BlockingConnection(self._conn_params)

        # create channel
        channel = conn.channel()

        # create exchange for pub/sub
        channel.exchange_declare(exchange=self._topic_name, exchange_type='fanout')

        # create dedicated queue for receiving message (create subscriber)
        queue_name = f'{self._topic_name}.{self._client_group}'
        channel.queue_declare(queue=queue_name, arguments={"x-dead-letter-exchange": self._topic_name})

        # bind created queue with pub/sub exchange
        channel.queue_bind(exchange=self._topic_name, queue=queue_name)

        # start consuming (blocking)
        print("Waiting incoming message. To exit press Ctrl+C")
        for message in channel.consume(queue=queue_name, auto_ack=False, inactivity_timeout=1):
            if self._is_interrupted:
                break

            if not message:
                continue

            method, properties, body = message

            if method is None:
                continue

            self._callback(channel, method, properties, body)

        print(f"[Process-{os.getpid()}] Consumer stopped")


class RabbitMqConsumerConfirm(ConsumerConfirm):

    def __init__(self, channel: Channel, delivery: Basic.Deliver):
        self._channel = channel
        self._delivery = delivery

    def ack(self):
        self._channel.basic_ack(self._delivery.delivery_tag)

    def nack(self):
        self._channel.basic_nack(self._delivery.delivery_tag)


def _callback_wrapper(callback):
    def fn(ch, method, properties, body):
        msg_dict = json.loads(body)
        msg = Message(msg_dict.get("headers"), msg_dict.get("body"))
        if type(msg.headers) == dict:
            msg.headers["x-rabbit"] = properties.headers
        callback(RabbitMqConsumerConfirm(ch, method), msg)

    return fn
