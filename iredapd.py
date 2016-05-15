# Author: Zhang Huangbin <zhb _at_ iredmail.org>

import os
import sys
import pwd
import socket
import asyncore
import asynchat

# Always remove 'settings.pyc'.
_pyc = os.path.abspath(os.path.dirname(__file__)) + '/settings.pyc'
if os.path.exists(_pyc):
    try:
        os.remove(_pyc)
    except:
        pass

del _pyc

# Import config file (settings.py) and modules
import settings
from libs import __version__, daemon
from libs import PLUGIN_PRIORITIES, SMTP_ACTIONS, SMTP_SESSION_ATTRIBUTES
from libs.logger import logger
from libs.utils import get_db_conn

# Plugin directory.
plugin_dir = os.path.abspath(os.path.dirname(__file__)) + '/plugins'
sys.path.append(plugin_dir)

if not settings.backend in ['ldap', 'mysql', 'pgsql']:
    sys.exit('Invalid backend, it must be ldap, mysql or pgsql.')

if settings.backend == 'ldap':
    from libs.ldaplib.modeler import Modeler

elif settings.backend in ['mysql', 'pgsql']:
    from libs.sql.modeler import Modeler


class PolicyChannel(asynchat.async_chat):
    """Process each smtp policy request"""
    def __init__(self,
                 sock,
                 db_conns=None,
                 plugins=[],
                 sender_search_attrlist=None,
                 recipient_search_attrlist=None):
        asynchat.async_chat.__init__(self, sock)
        self.buffer = []
        self.smtp_session_data = {}
        self.set_terminator('\n')

        self.db_conns = db_conns
        self.plugins = plugins
        self.sender_search_attrlist = sender_search_attrlist
        self.recipient_search_attrlist = recipient_search_attrlist

    def push(self, msg):
        asynchat.async_chat.push(self, msg + '\n')

    def collect_incoming_data(self, data):
        self.buffer.append(data)

    def found_terminator(self):
        if self.buffer:
            # Format received data
            line = self.buffer.pop()
            logger.debug("smtp session: " + line)
            if '=' in line:
                (key, value) = line.split('=', 1)

                if key in SMTP_SESSION_ATTRIBUTES:
                    if key in ['sender', 'recipient', 'sasl_username']:
                        # convert to lower cases.
                        v = value.lower()
                        self.smtp_session_data[key] = v

                        # Add sender_domain, recipient_domain, sasl_username_domain
                        self.smtp_session_data[key + '_domain'] = v.split('@', 1)[-1]
                    else:
                        self.smtp_session_data[key] = value
                else:
                    logger.debug('Drop invalid smtp session input: %s' % line)

        elif self.smtp_session_data:
            try:
                modeler = Modeler(conns=self.db_conns)
                result = modeler.handle_data(
                    smtp_session_data=self.smtp_session_data,
                    plugins=self.plugins,
                    sender_search_attrlist=self.sender_search_attrlist,
                    recipient_search_attrlist=self.recipient_search_attrlist,
                )
                if result:
                    action = result
                else:
                    action = SMTP_ACTIONS['default']
            except Exception, e:
                action = SMTP_ACTIONS['default']
                logger.error('Unexpected error: %s. Fallback to default action: %s' % (str(e), str(action)))

            self.push('action=' + action + '\n')
            logger.debug("Session ended")

            # Log sasl username, sender, recipient
            #   `sender -> recipient`: sender not authenticated
            #   `sender => recipient`: sasl username is same as sender address (From:)
            #   `sasl_username => sender -> recipient`: user send as different sender address
            _log_sender_to_rcpt = ''
            if self.smtp_session_data['sasl_username']:
                if self.smtp_session_data['sasl_username'] == self.smtp_session_data['sender']:
                    _log_sender_to_rcpt = '%s => %s' % (self.smtp_session_data['sasl_username'],
                                                        self.smtp_session_data['recipient'])
                else:
                    _log_sender_to_rcpt = '%s => %s -> %s' % (self.smtp_session_data['sasl_username'],
                                                              self.smtp_session_data['sender'],
                                                              self.smtp_session_data['recipient'])
            else:
                _log_sender_to_rcpt = '%s -> %s' % (self.smtp_session_data['sender'],
                                                    self.smtp_session_data['recipient'])

            # Log final action
            logger.info('[%s] %s, %s, %s' % (self.smtp_session_data['client_address'],
                                             self.smtp_session_data['protocol_state'],
                                             _log_sender_to_rcpt,
                                             action))
        else:
            action = SMTP_ACTIONS['default']
            logger.debug("replying: " + action)
            self.push('action=' + action + '\n')
            logger.debug("Session ended")


class DaemonSocket(asyncore.dispatcher):
    """Create socket daemon"""
    def __init__(self, local_addr, db_conns):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(local_addr)
        self.listen(5)
        ip, port = local_addr
        self.db_conns = db_conns

        logger.info("Starting iRedAPD (version: %s, backend: %s), listening on %s:%d." % (__version__, settings.backend, ip, port))

        # Rotate log file.
        if settings.LOGROTATE_TYPE == 'size':
            logger.info("Log rotate type: size (%d MB), backup copies: %d." % ((settings.LOGROTATE_SIZE / 1024 / 1024),
                                                                               settings.LOGROTATE_COPIES))
        elif settings.LOGROTATE_TYPE == 'time':
            logger.info("Log rotate type: time, interval: %s, backup copies: %d." % (settings.LOGROTATE_INTERVAL,
                                                                                     settings.LOGROTATE_COPIES))

        # Load plugins.
        self.loaded_plugins = []

        # Import priorities of built-in plugins.
        _plugin_priorities = PLUGIN_PRIORITIES

        # Import priorities of custom plugins, or custom built-in plugin priorities
        _plugin_priorities.update(settings.PLUGIN_PRIORITIES)   # third-party plugins

        # If enabled plugin doesn't have a priority pre-defined, set it to 0 (lowest)
        _plugins_without_priority = [p for p in settings.plugins if p not in _plugin_priorities]
        for _p in _plugins_without_priority:
            _plugin_priorities[_p] = 0

        # a list of {priority: name}
        pnl = []
        for p in settings.plugins:
            plugin_file = os.path.join(plugin_dir, p + '.py')
            if not os.path.isfile(plugin_file):
                logger.info('Plugin %s (%s) does not exist.' % (p, plugin_file))
                continue

            priority = _plugin_priorities[p]
            pnl += [{priority: p}]

        # Sort plugin order with pre-defined priorities, so that we can apply
        # plugins in ideal order.
        ordered_plugins = []
        for item in sorted(pnl, reverse=True):
            ordered_plugins += item.values()

        for plugin in ordered_plugins:
            try:
                self.loaded_plugins.append(__import__(plugin))
                logger.info('Loading plugin: %s (priority: %s)' % (plugin, _plugin_priorities[plugin]))
            except Exception, e:
                logger.error('Error while loading plugin (%s): %s' % (plugin, str(e)))

        self.sender_search_attrlist = []
        self.recipient_search_attrlist = []
        if settings.backend == 'ldap':
            self.sender_search_attrlist = ['objectClass']
            self.recipient_search_attrlist = ['objectClass']
            for plugin in self.loaded_plugins:
                try:
                    self.sender_search_attrlist += plugin.SENDER_SEARCH_ATTRLIST
                except:
                    pass

                try:
                    self.recipient_search_attrlist += plugin.RECIPIENT_SEARCH_ATTRLIST
                except:
                    pass

    def handle_accept(self):
        sock, remote_addr = self.accept()
        logger.debug("Connect from %s, port %s." % remote_addr)

        PolicyChannel(sock,
                      db_conns=self.db_conns,
                      plugins=self.loaded_plugins,
                      sender_search_attrlist=self.sender_search_attrlist,
                      recipient_search_attrlist=self.recipient_search_attrlist)


def main():
    # Set umask.
    os.umask(0077)

    if settings.backend in ['mysql', 'pgsql']:
        conn_vmail = get_db_conn('vmail')
    else:
        # we don't have ldap connection pool, a connection object will be
        # created in libs/ldaplib/modeler.py.
        conn_vmail = None

    conn_amavisd = get_db_conn('amavisd')
    conn_iredapd = get_db_conn('iredapd')

    db_conns = {'conn_vmail': conn_vmail,
                'conn_amavisd': conn_amavisd,
                'conn_iredapd': conn_iredapd}

    # Initialize policy daemon.
    local_addr = (settings.listen_address, int(settings.listen_port))
    DaemonSocket(local_addr, db_conns)

    # Run this program as daemon.
    try:
        daemon.daemonize(noClose=True)
    except Exception, e:
        logger.error('Error in daemon.daemonize: ' + str(e))

    # Write pid number into pid file.
    f = open(settings.pid_file, 'w')
    f.write(str(os.getpid()))
    f.close()

    # Get uid/gid of daemon user.
    p = pwd.getpwnam(settings.run_as_user)
    uid = p.pw_uid
    gid = p.pw_gid

    # Set log file owner
    os.chown(settings.log_file, uid, gid)
    os.chmod(settings.log_file, 0o700)

    # Run as daemon user
    os.setuid(uid)

    # Starting loop.
    try:
        # There's a bug report for Python 2.6/3.0 that `use_poll=True` yields
        # some 2.5 incompatibilities:
        if (sys.version_info >= (2, 7) and sys.version_info < (2, 8)) \
           or (sys.version_info >= (3, 4)):     # if python 2.7 ...
            # workaround for the "Bad file descriptor" issue on Python 2.7, gh-161
            asyncore.loop(use_poll=True)
        else:
            # fixes the "Unexpected communication problem" issue on Python 2.6 and 3.0
            asyncore.loop(use_poll=False)

    except KeyboardInterrupt:
        pass
    except Exception, e:
        logger.error('Error in asyncore.loop: ' + str(e))

if __name__ == '__main__':
    main()
