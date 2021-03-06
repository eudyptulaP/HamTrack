import os
import time
from datetime import datetime
import logging
import logging.config

import RPi.GPIO as GPIO

from peewee import *
from playhouse.pool import PooledMySQLDatabase

import threading

from pyfcm import FCMNotification
from requests import ConnectionError

# wheel circumference in cm
# e.g., for 2*r = 28cm, d = 2*pi*r
HAMSTER_WHEEL_CIRCUMFERENCE = 88
# min time between revolutions in ms
HAMSTER_DEBOUNCE = 250
# session timeout in s
HAMSTER_SESSION_TIMEOUT = 60

# GPIO stuff
GPIO_CHANNEL = 10

# setup logging
logging.config.fileConfig(os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    'logging_config.ini')
)
logger = logging.getLogger(__name__)

# SQL stuff
SQL_HOST = '<SQL_HOST>'
SQL_DB = '<SQL_DB>'
SQL_USER = '<SQL_USER>'
SQL_PASSWORD = '<SQL_PASSWORD>'
mysql_db = PooledMySQLDatabase(SQL_DB, host=SQL_HOST, user=SQL_USER, passwd=SQL_PASSWORD,
                               max_connections=8, stale_timeout=300)

# FCM stuff
# communicates with Android app that shows notifications
FCM_API_KEY = '<FCM_API_KEY>'

# Fallback file - written when sql fails
FALLBACK_FILE = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    'fallback.log'
)


# SQL table structure
class BaseModel(Model):
    """A base model that will use our MySQL database"""
    class Meta:
        database = mysql_db


class Hamstersession(BaseModel):
    start = DateTimeField(unique=True, index=True)
    circumference = FloatField()
    duration = FloatField()
    distance = FloatField()

def fallback_save(sessiondata):
    with open(FALLBACK_FILE, 'a') as fd:
        fd.write('---\n')  # to make it easily readable via pyaml
        fd.write('start: {0}\n'.format(sessiondata.start))
        fd.write('circumference: {0}\n'.format(sessiondata.circumference))
        fd.write('duration: {0}\n'.format(sessiondata.duration))
        fd.write('distance: {0}\n'.format(sessiondata.distance))

def execute_sql_query(sessiondata, retries=20, wait=30):
    while True:
        try:
            retries -= 1
            mysql_db.connect()
            sessiondata.save()
            mysql_db.close()
            logger.info("MySQL query successful")
            return True
        except OperationalError as err:
            mysql_db.close()
            logger.error("MySQL failed: %s", err)
            logger.error("Retries left: %d", retries)
            if retries == 0:
                logger.error("Writing fallback")
                fallback_save(sessiondata)
                return False
            time.sleep(wait)



class HamTrack(object):
    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(GPIO_CHANNEL, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

        # Create table if it doesn't exist
        if not Hamstersession.table_exists():
            Hamstersession.create_table()
            logger.info('Table Hamstersession created')

        # FCM stuff
        self.push_service = FCMNotification(api_key=FCM_API_KEY)

        # no. of revolutions
        self.revolutions = 0
        # timestamp
        self.last_revo = 0
        self.session_start = 0

        logger.info('HamTrack initialized')

    def post_notification(self, data_message):
        result = None

        try:
            result = self.push_service.notify_topic_subscribers(
                topic_name="news",
                data_message=data_message
            )
        except ConnectionError as err:
            logger.error("Could not send notification: %s", err)

        return result

    def finish_session(self, session_end):
        wstart = datetime.fromtimestamp(self.session_start)
        wcircumference = HAMSTER_WHEEL_CIRCUMFERENCE
        wduration = session_end - self.session_start
        wdistance = self.revolutions * HAMSTER_WHEEL_CIRCUMFERENCE

        logger.info('Session finished')
        logger.info('Start: {0}'.format(wstart))
        logger.info('Duration: {0}s'.format(wduration))
        logger.info('Distance: {0}m'.format(wdistance/100.0))
        logger.info('Revolutions: {0}'.format(self.revolutions))

        data_message = {
            "event": "session_finished",
            "start": int(self.session_start*1000),
            "duration": "{0:.1f}".format(wduration/60.0),
            "distance": "{0:.1f}".format(wdistance/100.0),
            "revolutions": self.revolutions
        }

        self.post_notification(data_message=data_message)

        hamstersession = Hamstersession(
            start=wstart,
            circumference=wcircumference,
            duration=wduration,
            distance=wdistance
        )

        t = threading.Thread(target=execute_sql_query, args=(hamstersession,))
        t.start()

    def start_session(self, session_start):
        data_message = {
            "event": "session_started",
            "start": int(session_start*1000)
        }
        self.post_notification(data_message=data_message)

    def run(self):
        while 1:
            channel = GPIO.wait_for_edge(GPIO_CHANNEL,
                                         GPIO.RISING,
                                         timeout=HAMSTER_SESSION_TIMEOUT*1000)
            now = time.time()
            if channel is None:
                # Timeout: End session
                if self.revolutions >= 5:
                    self.finish_session(now - HAMSTER_SESSION_TIMEOUT)
                if self.revolutions < 5 and self.session_start != 0:
                    logger.info('Session aborted - no activity')
                self.revolutions = 0
                self.session_start = 0
                continue

            # Debounce
            if now - self.last_revo > HAMSTER_DEBOUNCE/1000.0:
                # Revolution detected
                logger.debug('Revolution detected. dt=%.2f',
                             format(now-self.last_revo))
                # Start new session if none is running
                self.last_revo = now
                if self.session_start == 0:
                    self.session_start = now
                    logger.info('Session started')
                else:
                    self.revolutions += 1
                if self.revolutions == 5:
                    self.start_session(self.session_start)


if __name__ == "__main__":
    hamtrack = HamTrack()
    try:
        hamtrack.run()
    except KeyboardInterrupt:
        pass

    GPIO.cleanup()
