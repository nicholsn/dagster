import threading
from collections import namedtuple
from contextlib import contextmanager

import psycopg2
import sqlalchemy as db

from dagster import check
from dagster.core.events.log import EventRecord
from dagster.core.storage.event_log import (
    AssetAwareSqlEventLogStorage,
    SqlEventLogStorageMetadata,
    SqlEventLogStorageTable,
)
from dagster.core.storage.sql import create_engine, get_alembic_config, run_alembic_upgrade
from dagster.serdes import (
    ConfigurableClass,
    ConfigurableClassData,
    deserialize_json_to_dagster_namedtuple,
)

from ..pynotify import await_pg_notifications
from ..utils import pg_config, pg_url_from_config

CHANNEL_NAME = "run_events"

# Why? Because this is about as long as we expect a roundtrip to RDS to take.
WATCHER_POLL_INTERVAL = 0.2


class PostgresEventLogStorage(AssetAwareSqlEventLogStorage, ConfigurableClass):
    """Postgres-backed event log storage.

    Users should not directly instantiate this class; it is instantiated by internal machinery when
    ``dagit`` and ``dagster-graphql`` load, based on the values in the ``dagster.yaml`` file in
    ``$DAGSTER_HOME``. Configuration of this class should be done by setting values in that file.

    To use Postgres for event log storage, you can add a block such as the following to your
    ``dagster.yaml``:

    .. literalinclude:: ../../../../../examples/docs_snippets/docs_snippets/deploying/dagster-pg.yaml
       :caption: dagster.yaml
       :lines: 12-21
       :language: YAML

    Note that the fields in this config are :py:class:`~dagster.StringSource` and
    :py:class:`~dagster.IntSource` and can be configured from environment variables.

    """

    def __init__(self, postgres_url, inst_data=None):
        self.postgres_url = check.str_param(postgres_url, "postgres_url")
        self._event_watcher = PostgresEventWatcher(self.postgres_url)
        self._inst_data = check.opt_inst_param(inst_data, "inst_data", ConfigurableClassData)
        self._engine = create_engine(
            self.postgres_url, isolation_level="AUTOCOMMIT", poolclass=db.pool.NullPool
        )
        self._disposed = False
        SqlEventLogStorageMetadata.create_all(self._engine)

    def upgrade(self):
        alembic_config = get_alembic_config(__file__)
        run_alembic_upgrade(alembic_config, self._engine)

    @property
    def inst_data(self):
        return self._inst_data

    @classmethod
    def config_type(cls):
        return pg_config()

    @staticmethod
    def from_config_value(inst_data, config_value):
        return PostgresEventLogStorage(
            inst_data=inst_data, postgres_url=pg_url_from_config(config_value)
        )

    @staticmethod
    def create_clean_storage(conn_string):
        inst = PostgresEventLogStorage(conn_string)
        inst.wipe()
        return inst

    def store_event(self, event):
        """Store an event corresponding to a pipeline run.
        Args:
            event (EventRecord): The event to store.
        """
        check.inst_param(event, "event", EventRecord)
        sql_statement = self.prepare_insert_statement(event)  # from SqlEventLogStorage.py
        result_proxy = self._engine.execute(
            sql_statement.returning(SqlEventLogStorageTable.c.run_id, SqlEventLogStorageTable.c.id)
        )
        res = result_proxy.fetchone()
        result_proxy.close()
        self._engine.execute(
            """NOTIFY {channel}, %s; """.format(channel=CHANNEL_NAME),
            (res[0] + "_" + str(res[1]),),
        )

    @contextmanager
    def connect(self, run_id=None):
        yield self._engine

    def watch(self, run_id, start_cursor, callback):
        self._event_watcher.watch_run(run_id, start_cursor, callback)

    def end_watch(self, run_id, handler):
        self._event_watcher.unwatch_run(run_id, handler)

    @property
    def event_watcher(self):
        return self._event_watcher

    def __del__(self):
        # Keep the inherent limitations of __del__ in Python in mind!
        self.dispose()

    def dispose(self):
        if not self._disposed:
            self._disposed = True
            self._event_watcher.close()


EventWatcherProcessStartedEvent = namedtuple("EventWatcherProcessStartedEvent", "")
EventWatcherStart = namedtuple("EventWatcherStart", "")
EventWatcherEvent = namedtuple("EventWatcherEvent", "payload")
EventWatchFailed = namedtuple("EventWatchFailed", "message")
EventWatcherEnd = namedtuple("EventWatcherEnd", "")

EventWatcherThreadEvents = (
    EventWatcherProcessStartedEvent,
    EventWatcherStart,
    EventWatcherEvent,
    EventWatchFailed,
    EventWatcherEnd,
)
EventWatcherThreadNoopEvents = (EventWatcherProcessStartedEvent, EventWatcherStart)
EventWatcherThreadEndEvents = (EventWatchFailed, EventWatcherEnd)

POLLING_CADENCE = 0.25

TERMINATE_EVENT_LOOP = "TERMINATE_EVENT_LOOP"


def watcher_thread(conn_string, run_id_dict, handlers_dict, dict_lock, watcher_thread_exit):

    try:
        for notif in await_pg_notifications(
            conn_string,
            channels=[CHANNEL_NAME],
            timeout=POLLING_CADENCE,
            yield_on_timeout=True,
            exit_event=watcher_thread_exit,
        ):
            if notif is None:
                if watcher_thread_exit.is_set():
                    break
            else:
                run_id, index_str = notif.payload.split("_")
                if run_id not in run_id_dict:
                    continue

                index = int(index_str)
                with dict_lock:
                    handlers = handlers_dict.get(run_id, [])

                engine = create_engine(
                    conn_string, isolation_level="AUTOCOMMIT", poolclass=db.pool.NullPool
                )
                try:
                    res = engine.execute(
                        db.select([SqlEventLogStorageTable.c.event]).where(
                            SqlEventLogStorageTable.c.id == index
                        ),
                    )
                    dagster_event = deserialize_json_to_dagster_namedtuple(res.fetchone()[0])
                finally:
                    engine.dispose()

                for (cursor, callback) in handlers:
                    if index >= cursor:
                        callback(dagster_event)
    except psycopg2.OperationalError:
        pass


class PostgresEventWatcher(object):
    def __init__(self, conn_string):
        self._run_id_dict = {}
        self._handlers_dict = {}
        self._dict_lock = threading.Lock()
        self._conn_string = conn_string
        self._watcher_thread_exit = None
        self._watcher_thread = None

    def has_run_id(self, run_id):
        with self._dict_lock:
            _has_run_id = run_id in self._run_id_dict
        return _has_run_id

    def watch_run(self, run_id, start_cursor, callback):
        if not self._watcher_thread:
            self._watcher_thread_exit = threading.Event()
            self._watcher_thread = threading.Thread(
                target=watcher_thread,
                args=(
                    self._conn_string,
                    self._run_id_dict,
                    self._handlers_dict,
                    self._dict_lock,
                    self._watcher_thread_exit,
                ),
            )
            self._watcher_thread.daemon = True
            self._watcher_thread.start()

        with self._dict_lock:
            if run_id in self._run_id_dict:
                self._handlers_dict[run_id].append((start_cursor, callback))
            else:
                # See: https://docs.python.org/2/library/multiprocessing.html#multiprocessing.managers.SyncManager
                run_id_dict = self._run_id_dict
                run_id_dict[run_id] = None
                self._run_id_dict = run_id_dict
                self._handlers_dict[run_id] = [(start_cursor, callback)]

    def unwatch_run(self, run_id, handler):
        with self._dict_lock:
            if run_id in self._run_id_dict:
                self._handlers_dict[run_id] = [
                    (start_cursor, callback)
                    for (start_cursor, callback) in self._handlers_dict[run_id]
                    if callback != handler
                ]
            if not self._handlers_dict[run_id]:
                del self._handlers_dict[run_id]
                run_id_dict = self._run_id_dict
                del run_id_dict[run_id]
                self._run_id_dict = run_id_dict

    def close(self):
        if self._watcher_thread:
            self._watcher_thread_exit.set()
            self._watcher_thread.join()
            self._watcher_thread_exit = None
            self._watcher_thread = None
