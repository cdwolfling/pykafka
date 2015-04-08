import logging as log
from uuid import uuid4
import socket
import signal
import sys
import itertools
import time

from kazoo.exceptions import NoNodeException, NodeExistsError
from kazoo.client import KazooClient
from kazoo.recipe.watchers import ChildrenWatch

from kafka.pykafka.simpleconsumer import SimpleConsumer


class BalancedConsumer():
    def __init__(self,
                 topic,
                 cluster,
                 consumer_group,
                 zk_host='127.0.0.1:2181',
                 auto_commit_enable=False,
                 auto_commit_interval_ms=60 * 1000,
                 socket_timeout_ms=30000):
        """Create a BalancedConsumer

        Maintains a single instance of SimpleConsumer, periodically using the
        consumer rebalancing algorithm to reassign partitions to this
        SimpleConsumer.

        :param topic: the topic this consumer should consume
        :type topic: pykafka.topic.Topic
        :param cluster: the cluster this consumer should connect to
        :type cluster: pykafka.cluster.Cluster
        :param consumer_group: the name of the consumer group to join
        :type consumer_group: str
        :param zk_host: the ip and port of the zookeeper node to connect to
        :type zk_host: str
        :param auto_commit_enable: if true, periodically commit to kafka the
            offset of messages already fetched by this consumer
        :type auto_commit_enable: bool
        :param auto_commit_interval_ms: the frequency in ms that the consumer
            offsets are committed to kafka
        :type auto_commit_interval_ms: int
        :param socket_timeout_ms: the socket timeout for network requests
        :type socket_timeout_ms: int
        """
        self._cluster = cluster
        self._consumer_group = consumer_group
        self._topic = topic

        self._auto_commit_enable = auto_commit_enable
        self._auto_commit_interval_ms = auto_commit_interval_ms
        self._socket_timeout_ms = socket_timeout_ms

        self._consumer = None
        self._id = "{}:{}".format(socket.gethostname(), uuid4())
        self._partitions = set()
        self._setting_watches = True
        self._rebalance_retries = 5

        self._topic_path = '/consumers/{}/owners/{}'.format(self._consumer_group,
                                                            self._topic.name)
        self._id_path = '/consumers/{}/ids'.format(self._consumer_group)

        self._zookeeper = self._setup_zookeeper(zk_host)
        self._zookeeper.ensure_path(self._topic_path)
        self._add_self()
        self._set_watches()
        self._rebalance()

        def _close_zk_connection(signum, frame):
            self._zookeeper.stop()
            sys.exit()
        signal.signal(signal.SIGINT, _close_zk_connection)

    def _setup_zookeeper(self, zk_host):
        """Open a connection to a ZooKeeper host

        :param zk_host: the '<ip>:<port>' address of the zookeeper node to
            which to connect
        :type zk_host: str
        """
        zk = KazooClient(zk_host)
        zk.start()
        return zk

    def _setup_internal_consumer(self):
        """Create an internal SimpleConsumer instance

        If there is already a SimpleConsumer instance held by this object,
        stop its threads and mark it for garbage collection.
        """
        if self._consumer is not None:
            self._consumer.stop()
        self._consumer = SimpleConsumer(
            self._topic, self._cluster,
            consumer_group=self._consumer_group,
            partitions=list(self._partitions),
            auto_commit_enable=self._auto_commit_enable,
            auto_commit_interval_ms=self._auto_commit_interval_ms,
            socket_timeout_ms=self._socket_timeout_ms)

    def _decide_partitions(self, participants):
        """Decide which partitions belong to this consumer

        Uses the consumer rebalancing algorithm described here
        http://kafka.apache.org/documentation.html

        It is very important that the participants array is sorted,
        since this algorithm runs on each consumer and indexes into the same
        array.

        :param participants: sorted list of ids of the other consumers in this
            consumer group
        :type participants: list
        """
        # Freeze and sort partitions so we always have the same results
        p_to_str = lambda p: '-'.join([p.topic.name, str(p.leader.id), str(p.id)])
        all_partitions = list(self._topic.partitions.values())
        all_partitions.sort(key=p_to_str)

        # get start point, # of partitions, and remainder
        idx = participants.index(self._id)
        parts_per_consumer = len(all_partitions) / len(participants)
        remainder_ppc = len(all_partitions) % len(participants)

        start = parts_per_consumer * idx + min(idx, remainder_ppc)
        num_parts = parts_per_consumer + (0 if (idx + 1 > remainder_ppc) else 1)

        # assign partitions from i*N to (i+1)*N - 1 to consumer Ci
        new_partitions = itertools.islice(
            all_partitions,
            start,
            start + num_parts
        )
        new_partitions = set(new_partitions)
        log.info(
            'Balancing %i participants for %i partitions. '
            'My Partitions: %s -- Consumers: %s --- All Partitions: %s',
            len(participants), len(all_partitions),
            [p_to_str(p) for p in new_partitions],
            str(participants),
            [p_to_str(p) for p in all_partitions]
        )
        return new_partitions

    def _get_participants(self):
        """Use zookeeper to get the other consumers of this topic

        Returns a sorted list of the ids of the other consumers of self._topic
        """
        try:
            consumer_ids = self._zookeeper.get_children(self._id_path)
        except NoNodeException:
            log.debug("Consumer group doesn't exist. "
                      "No participants to find")
            return []

        participants = []
        for id_ in consumer_ids:
            try:
                topic, stat = self._zookeeper.get("%s/%s" % (self._id_path, id_))
                if topic == self._topic.name:
                    participants.append(id_)
            except NoNodeException:
                pass  # disappeared between ``get_children`` and ``get``
        participants.sort()
        return participants

    def _set_watches(self):
        """Set watches in zookeeper that will trigger rebalances

        Rebalances should be triggered whenever a broker, topic, or consumer
        znode is changed in ZooKeeper.
        """
        self._setting_watches = True
        # Set all our watches and then rebalance
        broker_path = '/brokers/ids'
        try:
            self._broker_watcher = ChildrenWatch(
                self._zookeeper, broker_path,
                self._brokers_changed
            )
        except NoNodeException:
            raise Exception(
                'The broker_path "%s" does not exist in your '
                'ZooKeeper cluster -- is your Kafka cluster running?'
                % broker_path)

        self._topics_watcher = ChildrenWatch(
            self._zookeeper,
            '/brokers/topics',
            self._topics_changed
        )

        self._consumer_watcher = ChildrenWatch(
            self._zookeeper, self._id_path,
            self._consumers_changed
        )
        self._setting_watches = False

    def _add_self(self):
        """Register this consumer in zookeeper

        Ensures we don't add more participants than partitions
        """
        participants = self._get_participants()
        if len(self._topic.partitions) <= len(participants):
            log.debug("More consumers than partitions.")
            return

        path = '{}/{}'.format(self._id_path, self._id)
        self._zookeeper.create(
            path, self._topic.name, ephemeral=True, makepath=True)

    def _rebalance(self):
        """Join a consumer group and claim partitions.

        Called whenever a ZooKeeper watch is triggered
        """
        log.info('Rebalancing consumer %s for topic %s.' % (
            self._id, self._topic.name)
        )

        participants = self._get_participants()
        new_partitions = self._decide_partitions(participants)

        for i in xrange(self._rebalance_retries):
            if i > 0:
                log.debug("Retrying in %is" % ((i + 1) ** 2))
                time.sleep(i ** 2)

                participants = self._get_participants()
                new_partitions = self._decide_partitions(participants)

            self._remove_partitions(self._partitions - new_partitions)

            try:
                self._add_partitions(new_partitions - self._partitions)
                break
            except NodeExistsError:
                continue

        self._setup_internal_consumer()

    def _path_from_partition(self, p):
        return "%s/%s-%s" % (self._topic_path, p.leader.id, p.id)

    def _remove_partitions(self, partitions):
        """Remove partitions from the ZK registry.

        :param partitions: partitions to remove.
        :type partitions: iterable of :class:kafka.pykafka.partition.Partition
        """
        for p in partitions:
            assert p in self._partitions
            self._zookeeper.delete(self._path_from_partition(p))
        self._partitions -= partitions

    def _add_partitions(self, partitions):
        """Add partitions to the ZK registry.

        :param partitions: partitions to add.
        :type partitions: iterable of :class:kafka.pykafka.partition.Partition
        """
        for p in partitions:
            self._zookeeper.create(
                self._path_from_partition(p), self._id,
                ephemeral=True
            )
        self._partitions |= partitions - self._partitions

    def _brokers_changed(self, brokers):
        if self._setting_watches:
            return
        self._rebalance()

    def _consumers_changed(self, consumers):
        if self._setting_watches:
            return
        self._rebalance()

    def _topics_changed(self, topics):
        if self._setting_watches:
            return
        self._rebalance()

    def consume(self):
        """Get one message from the consumer"""
        return self._consumer.consume()

    def __iter__(self):
        while True:
            yield self._consumer.consume()
