import base64
import json
import logging
import psycopg2
import time
import traceback
import dateutil.parser
import datetime
import os

from patroni.postgresql import PostgresConnectionException
from patroni.postgresql.misc import postgres_version_to_int, PostgresException
from patroni.utils import deep_compare, parse_bool, patch_config, Retry, \
    RetryFailedError, parse_int, split_host_port, tzutc
from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from six.moves.socketserver import ThreadingMixIn
from threading import Thread

logger = logging.getLogger(__name__)


def check_auth(func):
    """Decorator function to check authorization header.

    Usage example:
    @check_auth
    def do_PUT_foo():
        pass
    """
    def wrapper(handler, *args, **kwargs):
        if handler.check_auth_header():
            return func(handler, *args, **kwargs)
    return wrapper


class RestApiHandler(BaseHTTPRequestHandler):

    def _write_response(self, status_code, body, content_type='text/html', headers=None):
        self.send_response(status_code)
        headers = headers or {}
        if content_type:
            headers['Content-Type'] = content_type
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def _write_json_response(self, status_code, response):
        self._write_response(status_code, json.dumps(response), content_type='application/json')

    def send_auth_request(self, body):
        headers = {'WWW-Authenticate': 'Basic realm="' + self.server.patroni.__class__.__name__ + '"'}
        self._write_response(401, body, headers=headers)

    def check_auth_header(self):
        auth_header = self.headers.get('Authorization')
        status = self.server.check_auth_header(auth_header)
        return not status or self.send_auth_request(status)

    def _write_status_response(self, status_code, response):
        patroni = self.server.patroni
        tags = patroni.ha.get_effective_tags()
        if tags:
            response['tags'] = tags
        if patroni.postgresql.sysid:
            response['database_system_identifier'] = patroni.postgresql.sysid
        if patroni.postgresql.pending_restart:
            response['pending_restart'] = True
        response['patroni'] = {'version': patroni.version, 'scope': patroni.postgresql.scope}
        if patroni.scheduled_restart and isinstance(patroni.scheduled_restart, dict):
            response['scheduled_restart'] = patroni.scheduled_restart.copy()
            del response['scheduled_restart']['postmaster_start_time']
            response['scheduled_restart']['schedule'] = (response['scheduled_restart']['schedule']).isoformat()
        if not patroni.ha.watchdog.is_healthy:
            response['watchdog_failed'] = True
        if patroni.ha.is_paused():
            response['pause'] = True
        qsize = patroni.logger.queue_size
        if qsize > patroni.logger.NORMAL_LOG_QUEUE_SIZE:
            response['logger_queue_size'] = qsize
            lost = patroni.logger.records_lost
            if lost:
                response['logger_records_lost'] = lost
        self._write_json_response(status_code, response)

    def do_GET(self, write_status_code_only=False):
        """Default method for processing all GET requests which can not be routed to other methods"""

        time_start = time.time()
        request_type = 'OPTIONS' if write_status_code_only else 'GET'

        path = '/master' if self.path == '/' else self.path
        response = self.get_postgresql_status()

        patroni = self.server.patroni
        cluster = patroni.dcs.cluster

        if not cluster and patroni.ha.is_paused():
            primary_status_code = 200 if response['role'] == 'master' else 503
        else:
            primary_status_code = 200 if patroni.ha.is_leader() else 503

        replica_status_code = 200 if not patroni.noloadbalance and response.get('role') == 'replica' else 503
        status_code = 503

        if patroni.ha.is_standby_cluster() and ('standby_leader' in path or 'standby-leader' in path):
            status_code = 200 if patroni.ha.is_leader() else 503
        elif 'master' in path or 'leader' in path or 'primary' in path or 'read-write' in path:
            status_code = primary_status_code
        elif 'replica' in path:
            status_code = replica_status_code
        elif 'read-only' in path:
            status_code = 200 if primary_status_code == 200 else replica_status_code
        elif 'health' in path:
            status_code = 200 if response.get('state') == 'running' else 503
        elif cluster:  # dcs is available
            is_synchronous = cluster.is_synchronous_mode() and cluster.sync \
                    and cluster.sync.sync_standby == patroni.postgresql.name
            if path in ('/sync', '/synchronous') and is_synchronous:
                status_code = replica_status_code
            elif path in ('/async', '/asynchronous') and not is_synchronous:
                status_code = replica_status_code

        if write_status_code_only:  # when haproxy sends OPTIONS request it reads only status code and nothing more
            message = self.responses[status_code][0]
            self.wfile.write('{0} {1} {2}\r\n'.format(self.protocol_version, status_code, message).encode('utf-8'))
        else:
            self._write_status_response(status_code, response)

        time_end = time.time()
        self.log_message('%s %s %s latency: %s ms', request_type, path,
                         status_code, (time_end - time_start) * 1000)

    def do_OPTIONS(self):
        self.do_GET(write_status_code_only=True)

    def do_GET_patroni(self):
        response = self.get_postgresql_status(True)
        self._write_status_response(200, response)

    def do_GET_config(self):
        cluster = self.server.patroni.dcs.cluster or self.server.patroni.dcs.get_cluster()
        if cluster.config:
            self._write_json_response(200, cluster.config.data)
        else:
            self.send_error(502)

    def _read_json_content(self, body_is_optional=False):
        if 'content-length' not in self.headers:
            return self.send_error(411) if not body_is_optional else {}
        try:
            content_length = int(self.headers.get('content-length'))
            if content_length == 0 and body_is_optional:
                return {}
            request = json.loads(self.rfile.read(content_length).decode('utf-8'))
            if isinstance(request, dict) and (request or body_is_optional):
                return request
        except Exception:
            logger.exception('Bad request')
        self.send_error(400)

    @check_auth
    def do_PATCH_config(self):
        request = self._read_json_content()
        if request:
            cluster = self.server.patroni.dcs.get_cluster()
            data = cluster.config.data.copy()
            if patch_config(data, request):
                value = json.dumps(data, separators=(',', ':'))
                if not self.server.patroni.dcs.set_config_value(value, cluster.config.index):
                    return self.send_error(409)
            self.server.patroni.ha.wakeup()
            self._write_json_response(200, data)

    @check_auth
    def do_PUT_config(self):
        request = self._read_json_content()
        if request:
            cluster = self.server.patroni.dcs.get_cluster()
            if not deep_compare(request, cluster.config.data):
                value = json.dumps(request, separators=(',', ':'))
                if not self.server.patroni.dcs.set_config_value(value):
                    return self.send_error(502)
            self._write_json_response(200, request)

    @check_auth
    def do_POST_reload(self):
        try:
            if self.server.patroni.config.reload_local_configuration(True):
                status_code = 202
                response = 'reload scheduled'
                self.server.patroni.sighup_handler()
            else:
                status_code = 200
                response = 'nothing changed'
        except Exception as e:
            status_code = 500
            response = str(e)
        self._write_response(status_code, response)

    @staticmethod
    def parse_schedule(schedule, action):
        """ parses the given schedule and validates at """
        error = None
        scheduled_at = None
        try:
            scheduled_at = dateutil.parser.parse(schedule)
            if scheduled_at.tzinfo is None:
                error = 'Timezone information is mandatory for the scheduled {0}'.format(action)
                status_code = 400
            elif scheduled_at < datetime.datetime.now(tzutc):
                error = 'Cannot schedule {0} in the past'.format(action)
                status_code = 422
            else:
                status_code = None
        except (ValueError, TypeError):
            logger.exception('Invalid scheduled %s time: %s', action, schedule)
            error = 'Unable to parse scheduled timestamp. It should be in an unambiguous format, e.g. ISO 8601'
            status_code = 422
        return (status_code, error, scheduled_at)

    @check_auth
    def do_POST_restart(self):
        status_code = 500
        data = 'restart failed'
        request = self._read_json_content(body_is_optional=True)
        cluster = self.server.patroni.dcs.get_cluster()
        if request is None:
            # failed to parse the json
            return
        if request:
            logger.debug("received restart request: {0}".format(request))

        if cluster.is_paused() and 'schedule' in request:
            self._write_response(status_code, "Can't schedule restart in the paused state")
            return

        for k in request:
            if k == 'schedule':
                (_, data, request[k]) = self.parse_schedule(request[k], "restart")
                if _:
                    status_code = _
                    break
            elif k == 'role':
                if request[k] not in ('master', 'replica'):
                    status_code = 400
                    data = "PostgreSQL role should be either master or replica"
                    break
            elif k == 'postgres_version':
                try:
                    postgres_version_to_int(request[k])
                except PostgresException as e:
                    status_code = 400
                    data = e.value
                    break
            elif k == 'timeout':
                request[k] = parse_int(request[k], 's')
                if request[k] is None or request[k] <= 0:
                    status_code = 400
                    data = "Timeout should be a positive number of seconds"
                    break
            elif k != 'restart_pending':
                status_code = 400
                data = "Unknown filter for the scheduled restart: {0}".format(k)
                break
        else:
            if 'schedule' not in request:
                try:
                    status, data = self.server.patroni.ha.restart(request)
                    status_code = 200 if status else 503
                except Exception:
                    logger.exception('Exception during restart')
                    status_code = 400
            else:
                if self.server.patroni.ha.schedule_future_restart(request):
                    data = "Restart scheduled"
                    status_code = 202
                else:
                    data = "Another restart is already scheduled"
                    status_code = 409
        self._write_response(status_code, data)

    @check_auth
    def do_DELETE_restart(self):
        if self.server.patroni.ha.delete_future_restart():
            data = "scheduled restart deleted"
            code = 200
        else:
            data = "no restarts are scheduled"
            code = 404
        self._write_response(code, data)

    @check_auth
    def do_POST_reinitialize(self):
        request = self._read_json_content(body_is_optional=True)

        if request:
            logger.debug('received reinitialize request: %s', request)

        force = isinstance(request, dict) and parse_bool(request.get('force')) or False

        data = self.server.patroni.ha.reinitialize(force)
        if data is None:
            status_code = 200
            data = 'reinitialize started'
        else:
            status_code = 503
        self._write_response(status_code, data)

    def poll_failover_result(self, leader, candidate, action):
        timeout = max(10, self.server.patroni.dcs.loop_wait)
        for _ in range(0, timeout*2):
            time.sleep(1)
            try:
                cluster = self.server.patroni.dcs.get_cluster()
                if not cluster.is_unlocked() and cluster.leader.name != leader:
                    if not candidate or candidate == cluster.leader.name:
                        return 200, 'Successfully {0}ed over to "{1}"'.format(action[:-4], cluster.leader.name)
                    else:
                        return 200, '{0}ed over to "{1}" instead of "{2}"'.format(action[:-4].title(),
                                                                                  cluster.leader.name, candidate)
                if not cluster.failover:
                    return 503, action.title() + ' failed'
            except Exception as e:
                logger.debug('Exception occured during polling %s result: %s', action, e)
        return 503, action.title() + ' status unknown'

    def is_failover_possible(self, cluster, leader, candidate, action):
        if leader and (not cluster.leader or cluster.leader.name != leader):
            return 'leader name does not match'
        if candidate:
            if action == 'switchover' and cluster.is_synchronous_mode() and cluster.sync.sync_standby != candidate:
                return 'candidate name does not match with sync_standby'
            members = [m for m in cluster.members if m.name == candidate]
            if not members:
                return 'candidate does not exists'
        elif cluster.is_synchronous_mode():
            members = [m for m in cluster.members if m.name == cluster.sync.sync_standby]
            if not members:
                return action + ' is not possible: can not find sync_standby'
        else:
            members = [m for m in cluster.members if m.name != cluster.leader.name and m.api_url]
            if not members:
                return action + ' is not possible: cluster does not have members except leader'
        for st in self.server.patroni.ha.fetch_nodes_statuses(members):
            if st.failover_limitation() is None:
                return None
        return action + ' is not possible: no good candidates have been found'

    @check_auth
    def do_POST_failover(self, action='failover'):
        request = self._read_json_content()
        (status_code, data) = (400, '')
        if not request:
            return

        leader = request.get('leader')
        candidate = request.get('candidate') or request.get('member')
        scheduled_at = request.get('scheduled_at')
        cluster = self.server.patroni.dcs.get_cluster()

        logger.info("received %s request with leader=%s candidate=%s scheduled_at=%s",
                    action, leader, candidate, scheduled_at)

        if action == 'failover' and not candidate:
            data = 'Failover could be performed only to a specific candidate'
        elif action == 'switchover' and not leader:
            data = 'Switchover could be performed only from a specific leader'

        if not data and scheduled_at:
            if not leader:
                data = 'Scheduled {0} is possible only from a specific leader'.format(action)
            if not data and cluster.is_paused():
                data = "Can't schedule {0} in the paused state".format(action)
            if not data:
                (status_code, data, scheduled_at) = self.parse_schedule(scheduled_at, action)

        if not data and cluster.is_paused() and not candidate:
            data = action.title() + ' is possible only to a specific candidate in a paused state'

        if not data and not scheduled_at:
            data = self.is_failover_possible(cluster, leader, candidate, action)
            if data:
                status_code = 412

        if not data:
            if self.server.patroni.dcs.manual_failover(leader, candidate, scheduled_at=scheduled_at):
                self.server.patroni.ha.wakeup()
                if scheduled_at:
                    data = action.title() + ' scheduled'
                    status_code = 202
                else:
                    status_code, data = self.poll_failover_result(cluster.leader and cluster.leader.name,
                                                                  candidate, action)
            else:
                data = 'failed to write {0} key into DCS'.format(action)
                status_code = 503
        self._write_response(status_code, data)

    def do_POST_switchover(self):
        self.do_POST_failover(action='switchover')

    def parse_request(self):
        """Override parse_request method to enrich basic functionality of `BaseHTTPRequestHandler` class

        Original class can only invoke do_GET, do_POST, do_PUT, etc method implementations if they are defined.
        But we would like to have at least some simple routing mechanism, i.e.:
        GET /uri1/part2 request should invoke `do_GET_uri1()`
        POST /other should invoke `do_POST_other()`

        If the `do_<REQUEST_METHOD>_<first_part_url>` method does not exists we'll fallback to original behavior."""

        ret = BaseHTTPRequestHandler.parse_request(self)
        if ret:
            mname = self.path.lstrip('/').split('/')[0]
            mname = self.command + ('_' + mname if mname else '')
            if hasattr(self, 'do_' + mname):
                self.command = mname
        return ret

    def query(self, sql, *params, **kwargs):
        if not kwargs.get('retry', False):
            return self.server.query(sql, *params)
        retry = Retry(delay=1, retry_exceptions=PostgresConnectionException)
        return retry(self.server.query, sql, *params)

    def get_postgresql_status(self, retry=False):
        try:
            cluster = self.server.patroni.dcs.cluster

            if self.server.patroni.postgresql.state not in ('running', 'restarting', 'starting'):
                raise RetryFailedError('')
            stmt = ("WITH replication_info AS ("
                    "SELECT usename, application_name, client_addr, state, sync_state, sync_priority"
                    " FROM pg_catalog.pg_stat_replication) SELECT"
                    " pg_catalog.to_char(pg_catalog.pg_postmaster_start_time(), 'YYYY-MM-DD HH24:MI:SS.MS TZ'),"
                    " CASE WHEN pg_catalog.pg_is_in_recovery() THEN 0"
                    " ELSE ('x' || pg_catalog.substr(pg_catalog.pg_{0}file_name("
                    "pg_catalog.pg_current_{0}_{1}()), 1, 8))::bit(32)::int END,"
                    " CASE WHEN pg_catalog.pg_is_in_recovery() THEN 0"
                    " ELSE pg_catalog.pg_{0}_{1}_diff(pg_catalog.pg_current_{0}_{1}(), '0/0')::bigint END,"
                    " pg_catalog.pg_{0}_{1}_diff(COALESCE(pg_catalog.pg_last_{0}_receive_{1}(),"
                    " pg_catalog.pg_last_{0}_replay_{1}()), '0/0')::bigint,"
                    " pg_catalog.pg_{0}_{1}_diff(pg_catalog.pg_last_{0}_replay_{1}(), '0/0')::bigint,"
                    " pg_catalog.to_char(pg_catalog.pg_last_xact_replay_timestamp(), 'YYYY-MM-DD HH24:MI:SS.MS TZ'),"
                    " pg_catalog.pg_is_in_recovery() AND pg_catalog.pg_is_{0}_replay_paused(), "
                    "(SELECT pg_catalog.array_to_json(pg_catalog.array_agg("
                    "pg_catalog.row_to_json(ri))) FROM replication_info ri)")

            row = self.query(stmt.format(self.server.patroni.postgresql.wal_name,
                                         self.server.patroni.postgresql.lsn_name), retry=retry)[0]

            result = {
                'state': self.server.patroni.postgresql.state,
                'postmaster_start_time': row[0],
                'role': 'replica' if row[1] == 0 else 'master',
                'server_version': self.server.patroni.postgresql.server_version,
                'cluster_unlocked': bool(not cluster or cluster.is_unlocked()),
                'xlog': ({
                    'received_location': row[3],
                    'replayed_location': row[4],
                    'replayed_timestamp': row[5],
                    'paused': row[6]} if row[1] == 0 else {
                    'location': row[2]
                })
            }

            if result['role'] == 'replica' and self.server.patroni.ha.is_standby_cluster():
                result['role'] = self.server.patroni.postgresql.role

            if row[1] > 0:
                result['timeline'] = row[1]
            else:
                leader_timeline = None if not cluster or cluster.is_unlocked() else cluster.leader.timeline
                result['timeline'] = self.server.patroni.postgresql.replica_cached_timeline(leader_timeline)

            if row[7]:
                result['replication'] = row[7]

            return result
        except (psycopg2.Error, RetryFailedError, PostgresConnectionException):
            state = self.server.patroni.postgresql.state
            if state == 'running':
                logger.exception('get_postgresql_status')
                state = 'unknown'
            return {'state': state, 'role': self.server.patroni.postgresql.role}

    def log_message(self, fmt, *args):
        logger.debug("API thread: %s - - [%s] %s", self.client_address[0], self.log_date_time_string(), fmt % args)


class RestApiServer(ThreadingMixIn, HTTPServer, Thread):

    def __init__(self, patroni, config):
        self.patroni = patroni
        self.__listen = None
        self.__initialize(config)
        self.__set_config_parameters(config)
        self.daemon = True

    def query(self, sql, *params):
        cursor = None
        try:
            with self.patroni.postgresql.connection().cursor() as cursor:
                cursor.execute(sql, params)
                return [r for r in cursor]
        except psycopg2.Error as e:
            if cursor and cursor.connection.closed == 0:
                raise e
            raise PostgresConnectionException('connection problems')

    @staticmethod
    def _set_fd_cloexec(fd):
        if os.name != 'nt':
            import fcntl
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)

    def check_basic_auth_key(self, key):
        return self.__auth_key == key

    def check_auth_header(self, auth_header):
        if self.__auth_key:
            if auth_header is None:
                return 'no auth header received'
            if not auth_header.startswith('Basic ') or not self.check_basic_auth_key(auth_header[6:]):
                return 'not authenticated'

    @staticmethod
    def __get_ssl_options(config):
        return {option: config[option] for option in ['certfile', 'keyfile'] if option in config}

    def __set_config_parameters(self, config):
        self.__auth_key = base64.b64encode(config['auth'].encode('utf-8')).decode('utf-8') if 'auth' in config else None
        self.connection_string = '{0}://{1}/patroni'.format(self.__protocol,
                                                            config.get('connect_address') or self.__listen)

    def __initialize(self, config):
        try:
            host, port = split_host_port(config['listen'], None)
        except Exception:
            raise ValueError('Invalid "restapi" config: expected <HOST>:<PORT> for "listen", but got "{0}"'
                             .format(config['listen']))

        if self.__listen is not None:  # changing config in runtime
            self.shutdown()

        self.__listen = config['listen']
        self.__ssl_options = self.__get_ssl_options(config)

        HTTPServer.__init__(self, (host, port), RestApiHandler)
        Thread.__init__(self, target=self.serve_forever)
        self._set_fd_cloexec(self.socket)

        self.__protocol = 'http'

        # wrap socket with ssl if 'certfile' is defined in a config.yaml
        # Sometime it's also needed to pass reference to a 'keyfile'.
        if self.__ssl_options.get('certfile'):
            import ssl
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(**self.__ssl_options)
            self.socket = ctx.wrap_socket(self.socket, server_side=True)
            self.__protocol = 'https'
        return True

    def reload_config(self, config):
        if 'listen' not in config:  # changing config in runtime
            raise ValueError('Can not find "restapi.listen" config')

        elif (self.__listen != config['listen'] or self.__ssl_options != self.__get_ssl_options(config)) \
                and self.__initialize(config):
            self.start()
        self.__set_config_parameters(config)

    @staticmethod
    def handle_error(request, client_address):
        address, port = client_address
        logger.warning('Exception happened during processing of request from {}:{}'.format(address, port))
        logger.warning(traceback.format_exc())
