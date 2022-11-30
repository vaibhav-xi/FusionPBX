#!/usr/bin/env python
# vim: tabstop=4 softtabstop=4 shiftwidth=4 textwidth=80 smarttab expandtab
import sys
import os
import random
import signal
import time
import sched
import ESL
import logging
import uuid
from optparse import OptionParser

"""
TODO
- Implement -timeout (global timeout, we need to hupall all of our calls and exit)
- Implement fancy rate logic from sipp
- Implement sipp -trace-err option
- Implement sipp return codes (0 for success, 1 call failed, etc etc)
"""

class FastScheduler(sched.scheduler):

    def __init__(self, timefunc, delayfunc):
        self.queue = []
        # Do not use super() as sched.scheduler does not inherit from object
        sched.scheduler.__init__(self, timefunc, delayfunc)
        if sys.version_info[0] == 2 and sys.version_info[1] >= 6:
            """
            Python 2.6 renamed the sched member queue to _queue, lets
            keep the old name in our class
            """
            self.queue = self._queue

    def next_event_time_delta(self):
        """
        Return the time delta in seconds for the next event
        to become ready for execution
        """
        q = self.queue
        if len(q) <= 0:
            return -1
        time, priority, action, argument = q[0]
        now = self.timefunc()
        if time > now:
            return int(time - now)
        return 0

    def fast_run(self):
        """
        Try to run events that are ready only and return immediately
        It is assumed that the callbacks will not block and the time
        is only retrieved once (when entering the function) and not
        before executing each event, so there is a chance an event
        that becomes ready while looping will not get executed
        """
        q = self.queue
        now = self.timefunc()
        while q:
            time, priority, action, argument = q[0]
            if now < time:
                break
            if now >= time:
                self.cancel(q[0])
                void = action(*argument)

class Session(object):
    def __init__(self, uuid):
        self.uuid = uuid
        self.partner_uuid = None
        self.created = False
        self.answered = False
        self.bert_sync_lost_cnt = 0
        self.bert_timeout = False

class SessionManager(object):
    def __init__(self, server, port, auth, logger,
            rate=1, limit=1, max_sessions=0, duration=60, random=0,
            originate_string='', debug=False, dtmf_seq=None, dtmf_delay=1,
            time_rate=1,report_interval=0):
        self.server = server
        self.port = port
        self.auth = auth
        self.rate = rate
        self.limit = limit
        self.max_sessions = max_sessions
        self.dtmf_seq = dtmf_seq
        self.dtmf_delay = dtmf_delay
        self.duration = duration
        self.random = random
        self.time_rate = time_rate
        self.report_interval = report_interval
        self.originate_string = originate_string
        self.logger = logger
        self.test_id_var = 'fs_test'
        self.test_id = uuid.uuid1()
        self.test_uuid_x_header = 'sip_h_X-fs_test_uuid'
        self.bert_sync_lost_var = 'bert_stats_sync_lost'

        self.sessions = {}
        self.peer_sessions = {}
        self.hangup_causes = {}
        self.total_originated_sessions = 0
        self.total_answered_sessions = 0
        self.total_failed_sessions = 0
        self.terminate = False
        self.paused = 0
        self.ev_handlers = {
            'CHANNEL_ORIGINATE': self.handle_originate,
            'CHANNEL_CREATE': self.handle_create,
            'CHANNEL_ANSWER': self.handle_answer,
            'CHANNEL_BRIDGE': self.handle_answer,
            'CHANNEL_HANGUP': self.handle_hangup,
            'SERVER_DISCONNECTED': self.handle_disconnect,
            'CUSTOM': self.handle_custom,
        }
        self.custom_ev_handlers = {
            'mod_bert::timeout': self.handle_bert_timeout,
            'mod_bert::lost_sync': self.handle_bert_lost_sync,
        }

        signal.signal(signal.SIGTSTP, self.pause_resume_calls)

        self.sched = FastScheduler(time.time, time.sleep)
        # Initialize the ESL connection
        self.con = ESL.ESLconnection(self.server, self.port, self.auth)
        if not self.con.connected():
            logger.error('Failed to connect!')
            raise Exception

        # Raise the sps and max_sessions limit to make sure they do not obstruct our test
        self.con.api('fsctl sps %d' % 100000)
        self.con.api('fsctl max_sessions %d' % 100000)
        self.con.api('fsctl verbose_events true')

        # Reduce logging level to avoid much output in console/logfile
        if debug:
            self.con.api('fsctl loglevel debug')
            self.con.api('console loglevel debug')
            logger.setLevel(logging.DEBUG)
        else:
            self.con.api('fsctl loglevel warning')
            self.con.api('console loglevel warning')

        # Make sure latest XML is loaded
        self.con.api('reloadxml')

        # Register relevant events to get notified about our sessions created/destroyed
        #self.con.events('plain', 'CHANNEL_ORIGINATE CHANNEL_ANSWER CHANNEL_HANGUP CUSTOM')
        for key, val in self.ev_handlers.iteritems():
            self.con.events('plain', key)
        for key, val in self.custom_ev_handlers.iteritems():
            evstr = 'CUSTOM %s' % (key)
            self.con.events('plain', evstr)

        # Fix up the originate string to add our identifier
        if self.originate_string[0] == '{':
            self.originate_string = '{%s=%s,%s' % (self.test_id_var, str(self.test_id), self.originate_string[1:])
        else:
            self.originate_string = '{%s=%s}%s' % (self.test_id_var, str(self.test_id), self.originate_string)
        self.logger.debug('Originate string: %s' % self.originate_string)

    def pause_resume_calls(self, signum, frame):
        if self.paused:
            self.paused = 0
        else:
            self.paused = 1

    def originate_sessions(self):
        if self.paused:
            if self.paused == 1:
                self.logger.info('... Paused ...')
            self.paused = self.paused + 1
            self.sched.enter(1, 1, self.originate_sessions, [])
            return
        if not self.con.connected():
            self.reconnect()
        self.logger.debug('Originating sessions')
        if self.max_sessions and self.total_originated_sessions >= self.max_sessions:
            self.logger.info('Done originating sessions')
            return
        sesscnt = len(self.sessions)
        originated_sessions = 0
        for i in range(0, self.rate):
            if sesscnt >= self.limit:
                break
            originate_uuid = uuid.uuid1()
            originate_string = '{origination_uuid=%s,%s=%s,%s' % (str(originate_uuid),
                                self.test_uuid_x_header, str(originate_uuid), self.originate_string[1:])
            self.sessions[str(originate_uuid)] = Session(originate_uuid)
            self.con.api('bgapi originate %s' % (originate_string))
            sesscnt = sesscnt + 1
            originated_sessions = originated_sessions + 1
            self.logger.debug('Requested session %s (%s)' % (originate_uuid, originate_string))
        if originated_sessions:
            self.logger.info('Originated %d new sessions', originated_sessions)
        self.logger.debug('Done originating sessions')
        self.sched.enter(self.time_rate, 1, self.originate_sessions, [])

    def process_event(self, e):
        evname = e.getHeader('Event-Name')
        if evname in self.ev_handlers:
            try:
                self.ev_handlers[evname](e)
            except Exception, ex:
                self.logger.error('Failed to process event %s: %s' % (evname, ex))
        else:
            self.logger.error('Unknown event %s' % (evname))

    def handle_custom(self, e):
        evname = e.getHeader('Event-Name')
        subclass = e.getHeader('Event-Subclass')
        if subclass in self.custom_ev_handlers:
            try:
                self.custom_ev_handlers[subclass](e)
            except Exception, ex:
                self.logger.error('Failed to process event %s/%s: %s' % (evname, subclass, ex))
        else:
            self.logger.error('Unknown event %s/%s' % (evname, subclass))

    def handle_create(self, e):
        uuid = e.getHeader('Unique-ID')
        self.logger.debug('Created session %s' % uuid)
        if uuid in self.sessions:
            return
        var_uuid = 'variable_%s' % (self.test_uuid_x_header)
        partner_uuid = e.getHeader(var_uuid)
        if not partner_uuid:
            return
        if partner_uuid not in self.sessions:
            return
        self.logger.debug('UUID %s is bridged to UUID %s' % (uuid, partner_uuid))
        self.sessions[partner_uuid].partner_uuid = uuid
        self.peer_sessions[uuid] = self.sessions[partner_uuid]
        self.con.api('uuid_set_var %s %s %s' % (uuid, self.test_id_var, self.test_id))

    def handle_originate(self, e):
        uuid = e.getHeader('Unique-ID')
        if uuid not in self.sessions:
            # Ignore call we did not originate
            return
        self.logger.debug('Originated session %s' % uuid)
        self.sessions[uuid].created = True
        self.total_originated_sessions = self.total_originated_sessions + 1
        if self.random:
            duration = random.randint(self.random, self.duration)
        else:
            duration = self.duration
        self.logger.debug('Calculated duration %d for uuid %s' %
            (duration,uuid))
        self.con.api('sched_hangup +%d %s NORMAL_CLEARING' % (duration, uuid))
        if self.dtmf_seq:
            self.logger.debug('Scheduling DTMF %s with delay %d at uuid %s' % (self.dtmf_seq, self.dtmf_delay, uuid))
            self.con.api('sched_api +%d none uuid_send_dtmf %s %s' % (self.dtmf_delay, uuid, self.dtmf_seq))
        if self.report_interval and not self.total_originated_sessions % self.report_interval:
            self.report()

    def handle_answer(self, e):
        uuid = e.getHeader('Unique-ID')
        if uuid not in self.sessions:
            return
        self.logger.debug('Answered session %s' % uuid)
        self.total_answered_sessions = self.total_answered_sessions + 1
        self.sessions[uuid].answered = True

    def handle_hangup(self, e):
        uuid = e.getHeader('Unique-ID')
        if uuid not in self.sessions:
            return
        cause = e.getHeader('Hangup-Cause')
        if cause not in self.hangup_causes:
            self.hangup_causes[cause] = 1
        else:
            self.hangup_causes[cause] = self.hangup_causes[cause] + 1
        if not self.sessions[uuid].answered:
            self.total_failed_sessions = self.total_failed_sessions + 1
        del self.sessions[uuid]
        self.logger.debug('Hung up session %s' % uuid)
        if (self.max_sessions and self.total_originated_sessions >= self.max_sessions \
            and len(self.sessions) == 0):
            self.terminate = True

    def handle_bert_lost_sync(self, e):
        uuid = e.getHeader('Unique-ID')
        if uuid not in self.sessions:
            if uuid not in self.peer_sessions:
                return
            sess = self.peer_sessions[uuid]
            partner_uuid = sess.uuid
        else:
            sess = self.sessions[uuid]
            partner_uuid = sess.partner_uuid
        self.logger.error('BERT Lost Sync on session %s' % uuid)
        sess.bert_sync_lost_cnt = sess.bert_sync_lost_cnt + 1
        if sess.bert_sync_lost_cnt > 1:
            return
        # Since mod_bert does not know about the peer session, we set the var ourselves
        self.con.api('uuid_set_var %s %s true' % (uuid, self.bert_sync_lost_var))
        self.con.api('uuid_set_var %s %s true' % (partner_uuid, self.bert_sync_lost_var))

    def handle_bert_timeout(self, e):
        uuid = e.getHeader('Unique-ID')
        if uuid not in self.sessions:
            return
        self.logger.error('BERT Timeout on session %s' % uuid)
        self.sessions[uuid].bert_timeout = True

    def handle_disconnect(self):
        self.logger.error('Disconnected from server!')
        self.terminate = True

    def hupall(self):
        self.con.api('bgapi hupall NORMAL_CLEARING %s %s' % (self.test_id_var, str(self.test_id)))

    def run(self):
        self.originate_sessions()
        try:
            while True:
                self.sched.fast_run()
                e = self.con.recvEventTimed(100)
                if e is None:
                    continue
                self.process_event(e)
                if self.terminate:
                    break
        except:
            self.reconnect()
            self.hupall()
            raise

    def reconnect(self):
        if self.con.connected():
            return
        self.con = ESL.ESLconnection(self.server, self.port, self.auth)
        if not self.con.connected():
            logger.error('Failed to re-connect!')
            self.terminate = True

    def report(self):
        self.logger.info('Total originated sessions: %d' % self.total_originated_sessions)
        self.logger.info('Total answered sessions: %d' % self.total_answered_sessions)
        self.logger.info('Total failed sessions: %d' % self.total_failed_sessions)
        self.logger.info('-- Call Hangup Stats --')
        for cause, count in self.hangup_causes.iteritems():
            self.logger.info('%s: %d' % (cause, count))
        self.logger.info('-----------------------')

def main(argv):

    formatter = logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
    logger = logging.getLogger(os.path.basename(sys.argv[0]))
    logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Try to emulate sipp options (-r, -l, -d, -m)
    parser = OptionParser()
    parser.add_option("-a", "--auth", dest="auth", default="ClueCon",
                    help="ESL password")
    parser.add_option("-s", "--server", dest="server", default="127.0.0.1",
                    help="FreeSWITCH server IP address")
    parser.add_option("-p", "--port", dest="port", default="8021",
                    help="FreeSWITCH server event socket port")
    parser.add_option("-r", "--rate", dest="rate", default=1,
                    help="Rate in sessions to run per unit of time (see --time-rate)")
    parser.add_option("-t", "--time-rate", dest="time_rate", default=1,
                    help="Time rate in seconds")
    parser.add_option("-l", "--limit", dest="limit", default=1,
                    help="Limit max number of concurrent sessions")
    parser.add_option("-d", "--duration", dest="duration", default=60,
                    help="Max duration in seconds of sessions before being hung up")
    parser.add_option("", "--random", dest="random", default=0,
                    help="Randomize duration with minimum number of seconds")
    parser.add_option("-m", "--max-sessions", dest="max_sessions", default=0,
                    help="Max number of sessions to originate before stopping")
    parser.add_option("-o", "--originate-string", dest="originate_string",
                    help="FreeSWITCH originate string")
    parser.add_option("", "--debug", dest="debug", action="store_true",
                    help="Enable debugging")
    parser.add_option("", "--dtmf-seq", dest="dtmf_seq", default=None,
                    help="Play the given DTMF sequence after answer")
    parser.add_option("", "--dtmf-delay", dest="dtmf_delay", default=1,
                    help="How long to wait after answer to play DTMF")
    parser.add_option("", "--sleep", dest="sleeptime", default=0,
                    help="Number of seconds to sleep before starting")
    parser.add_option("", "--report", dest="report_interval", default=0,
                    help="Number of originates between cumalative reports")

    (options, args) = parser.parse_args()

    if not options.originate_string:
        sys.stderr.write('-o is mandatory\n')
        sys.exit(1)

    if options.random > options.duration:
        sys.stderr.write('random minimum cannot be more than duration\n')
        sys.exit(1)

    if options.sleeptime:
        sys.stderr.write('Sleeping for %s seconds...\n' % options.sleeptime)
        time.sleep(float(options.sleeptime))

    sm = SessionManager(options.server, options.port, options.auth, logger,
            rate=int(options.rate), limit=int(options.limit),
            duration=int(options.duration), random=int(options.random),
            max_sessions=int(options.max_sessions),
            originate_string=options.originate_string,
            debug=options.debug, dtmf_seq=options.dtmf_seq,
            dtmf_delay=int(options.dtmf_delay),
            time_rate=int(options.time_rate),
            report_interval=int(options.report_interval))

    try:
        sm.run()
    except KeyboardInterrupt:
        pass

    sm.report()

    if sm.total_failed_sessions:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception, e:
        sys.stderr.write("Exception caught: %s\n" % (e))
        raise