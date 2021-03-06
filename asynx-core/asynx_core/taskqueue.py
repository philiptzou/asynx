# -*- coding: utf-8 -*-

import re
import copy
import weakref
import inspect
from itertools import islice
from datetime import timedelta

import celery
import requests
from pytz import utc
from dateutil import parser
from tzlocal import get_localzone
from celery import schedules
from redis import WatchError

from ._util import (_dumps, _loads, dict_items, basestring, utcnow,
                    get_total_seconds, not_bytes, user_agent)


class TaskAlreadyExists(Exception):
    pass


class TaskNotFound(Exception):
    pass


class TaskStatusNotMatched(Exception):
    pass


class TaskCNameRequired(Exception):
    pass


@celery.shared_task()
def request_task(tq_class, appname, queuename, task_id):
    """Dispatch an HTTP request task."""
    tq = tq_class(appname, queuename)
    try:
        task = tq._get_task(task_id)
    except TaskNotFound:
        return
    task.dispatch()


class TaskQueue(object):

    def __init__(self, appname, queuename='default', localzone=None):
        """Initialize a TaskQueue object

        Parameters:
            - appname: string, application's name
            - queuename: string, queue's name, default "default"
            - localzone: timezone object, the local timezone to be
                         used if the incoming datetime is naive

        Usage:
            >>> import redis
            >>> conn = redis.StrictRedis()
            >>> tq = TaskQueue('test')
            >>> tq.bind_redis(conn)

        """
        self.appname = appname
        self.queuename = queuename
        self.localzone = localzone or get_localzone()
        self._redis = None

    @property
    def redis(self):
        """The bound redis connection"""
        if self._redis is None:
            raise RuntimeError('taskqueue is not bound '
                               'with a redis connection')
        return self._redis

    def bind_redis(self, connection):
        """Binding redis connection

        Usage:
            >>> import redis
            >>> conn = redis.StrictRedis()
            >>> tq = TaskQueue('test')
            >>> tq.bind_redis(conn)
            >>> assert isinstance(tq.redis, redis.StrictRedis)

            >>> tq = TaskQueue('test2')
            >>> tq.redis
            Traceback (most recent call last):
                ...
            RuntimeError: taskqueue is not bound with a redis connection

        """
        self._redis = connection

    def __hincrkey(self):
        """generating an auto-increment key per queue for every app

        Doctest:
            >>> tq = TaskQueue('test', 'custom')
            >>> tq._TaskQueue__hincrkey()
            ('AX:INC', 'test:custom')

        """

        return 'AX:INC', '{0}:{1}'.format(self.appname, self.queuename)

    def __schedkey(self):
        """generates a key listing all scheduled tasks

        Doctest:
            >>> tq = TaskQueue('test', 'custom')
            >>> tq._TaskQueue__schedkey()
            'AX:SC:test:custom'

        """
        return 'AX:SC:{0}:{1}'.format(self.appname, self.queuename)

    def __metakey(self, idx):
        """generating a metakey to store task's metadata

        Doctest:
            >>> tq = TaskQueue('test', 'custom')
            >>> tq._TaskQueue__metakey(12345)
            'AX:META:test:custom:12345'

        """
        return 'AX:META:{0}:{1}:{2}'.format(self.appname, self.queuename, idx)

    def __cnamekey(self, cname):
        """generating a cname key mapping a task

        Doctest:
            >>> tq = TaskQueue('test', 'custom')
            >>> tq._TaskQueue__cnamekey('task001')
            'AX:CNAME:test:custom:task001'

        """
        return 'AX:CNAME:{0}:{1}:{2}'.format(self.appname,
                                             self.queuename,
                                             cname)

    def __uuidkey(self):
        """generating a sorted set key mapping uuid to task

        Doctest:
            >>> tq = TaskQueue('test', 'custom')
            >>> tq._TaskQueue__uuidkey()
            'AX:UUID:test:custom'

        """
        return 'AX:UUID:{0}:{1}'.format(self.appname, self.queuename)

    def _dispatch_task(self, task):
        """dispatching a "new" task into celery queue

        Parameters:
            - task: a Task object with status == 'new'

        """
        old_uuid = task.uuid
        task.apply_async(task.last_run_at or utcnow())
        update_fields = {
            'uuid': task.uuid,
            'status': task.status
        }
        for key, val in dict_items(update_fields):
            update_fields[key] = _dumps(val)
        uuidkey = self.__uuidkey()
        metakey = self.__metakey(task.id)
        with self.redis.pipeline() as pipe:
            pipe.hmset(metakey, update_fields)
            if old_uuid:
                pipe.zrem(uuidkey, old_uuid)
            pipe.zadd(uuidkey, task.id, task.uuid)
            pipe.execute()

    def add_task(self, request, cname=None,
                 countdown=None, eta=None,
                 schedule=None, on_success=None,
                 on_failure='__report__',
                 on_complete=None):
        """adding and dispatch task

        Parameters:
            - request: a dict contains request arguments:
                method, url, headers(dict), payload(string),
                timeout, allow_redirects(bool)
            - cname: optional, string, custom task name
            - countdown: optional, int/float in seconds
            - eta: optional, datatime object
            - schedule: optional, celery schedule/crontab object
            - on_success: callback when success, can be None,
                          a url, an internal method or subtask dict
            - on_failure: callback when failure
            - on_complete: callback when complete

        Returns:
            task dict

        """
        if eta and eta.tzinfo is None:
            # a naive timestamp, localize it
            eta = self.localzone.localize(eta)
        if schedule and not cname:
            raise TaskCNameRequired('Scheduled task must have a custom name')
        task = Task(request=request, cname=cname,
                    countdown=countdown, eta=eta,
                    schedule=schedule,
                    on_success=on_success,
                    on_failure=on_failure,
                    on_complete=on_complete)
        incrkey, incrhash = self.__hincrkey()
        with self.redis.pipeline() as pipe:
            try:
                if task.cname:
                    cname = task.cname
                    cnamekey = self.__cnamekey(cname)
                    pipe.watch(cnamekey)
                    exists = pipe.exists(cnamekey)
                    if exists:
                        raise TaskAlreadyExists(
                            'task "{0}" is already exists (1)'.format(cname))
                    pipe.multi()
                task.id = idx = self.redis.hincrby(incrkey, incrhash)
                if task.cname:
                    pipe.set(cnamekey, idx)
                metakey = self.__metakey(idx)
                _, task_dict = task._to_redis()
                pipe.hmset(metakey, task_dict)
                if task.schedule:
                    schedkey = self.__schedkey()
                    pipe.zadd(schedkey, 0, idx)
                pipe.execute()
            except WatchError:
                raise TaskAlreadyExists(
                    'task "{0}" is already exists (2)'.format(cname))
        task.bind_taskqueue(self)
        self._dispatch_task(task)
        return task.to_dict()

    def iter_tasks(self, offset=0, per_pipeline=50):
        """iterating tasks start from offset

        Parameters:
            - offset: integer, where the iteration started
            - per_pipeline: integer, how mush tasks to fetch per pipeline

        Returns:
            a generator iterating dict of tasks

        """
        uuidkey = self.__uuidkey()
        while 1:
            result = self.redis.zrange(uuidkey, offset,
                                       offset + per_pipeline - 1,
                                       withscores=True, score_cast_func=int)
            if not result:
                break
            with self.redis.pipeline() as pipe:
                for uuid, idx in result:
                    metakey = self.__metakey(idx)
                    pipe.hgetall(metakey)
                tasks = pipe.execute()
            for uuid_idx, task_dict in zip(result, tasks):
                if task_dict is None:
                    continue  # wtf?!
                uuid, idx = uuid_idx
                yield Task._from_redis(idx, task_dict).to_dict()
            if len(result) < per_pipeline:
                break
            offset += per_pipeline

    def count_tasks(self):
        """counting tasks

        Returns:
            integer, count of all tasks in the queue

        """
        uuidkey = self.__uuidkey()
        return self.redis.zcard(uuidkey)

    def list_tasks(self, offset=0, limit=50):
        """listing tasks with offset and limit

        Parameters:
            - offset: integer
            - limit: integer

        Returns:
            a list of tasks (dict)

        """
        per_pipeline = min(limit + 10, 100)
        tasks = self.iter_tasks(offset, per_pipeline)
        return list(islice(tasks, 0, limit))

    def _get_task(self, task_id):
        """retrieving task by task_id

        Do not use this method directly, use get_task instead

        returns Task object

        """
        metakey = self.__metakey(task_id)
        task_dict = self.redis.hgetall(metakey)
        if not task_dict:
            raise TaskNotFound('task "{0}" is not exist (r)'
                               .format(task_id))
        task = Task._from_redis(task_id, task_dict)
        task.bind_taskqueue(self)
        return task

    def get_task(self, task_id):
        """retrieving task by task id

        Parameters:
            - task_id: integer, task id

        Returns:
            task dict

        """
        return self._get_task(task_id).to_dict()

    def _get_task_by_uuid(self, uuid):
        uuidkey = self.__uuidkey()
        task_id = self.redis.zscore(uuidkey, uuid)
        if not task_id:
            raise TaskNotFound('task with uuid "{0}" is not found'
                               .format(uuid))
        return self._get_task(int(task_id))

    def get_task_by_uuid(self, uuid):
        """retrieving task by task uuid

        Parameters:
            - uuid: string, task's uuid

        Returns:
            task dict

        """
        return self._get_task_by_uuid(uuid).to_dict()

    def _get_task_by_cname(self, cname):
        cnamekey = self.__cnamekey(cname)
        task_id = self.redis.get(cnamekey)
        if not task_id:
            raise TaskNotFound('task with cname "{0}" is not found'
                               .format(cname))
        return self._get_task(int(task_id))

    def get_task_by_cname(self, cname):
        """retrieving task by task cname

        Parameters:
            - cname: string, task's cname

        Returns:
            task dict

        """
        return self._get_task_by_cname(cname).to_dict()

    def _delete_task(self, task):
        """deleting task

        Do not use this method directly, use delete_task instead

        """
        metakey = self.__metakey(task.id)
        uuidkey = self.__uuidkey()
        cnamekey = None
        schedkey = None
        if task.cname:
            cnamekey = self.__cnamekey(task.cname)
        if task.schedule:
            schedkey = self.__schedkey()

        def __delete_task(pipe):
            pipe.multi()
            pipe.delete(metakey)
            pipe.zrem(uuidkey, task.uuid)
            if cnamekey:
                pipe.delete(cnamekey)
            if schedkey:
                pipe.zrem(schedkey, task.id)
        self.redis.transaction(__delete_task, metakey, uuidkey, cnamekey)

    def delete_task(self, task_id):
        """deleting task by task id

        Parameters:
            - task_id: integer, task id

        """
        task = self._get_task(task_id)
        if task.status == 'running':
            raise TaskStatusNotMatched('task "{0}" can not be deleted '
                                       'because it is running'.format(task.id))
        self._delete_task(task)

    def delete_task_by_uuid(self, uuid):
        """deleting task by task uuid

        Parameters:
            - uuid: string, task's uuid

        """
        task = self._get_task_by_uuid(uuid)
        self._delete_task(task)

    def delete_task_by_cname(self, cname):
        """deleting task by task cname

        Parameters:
            - cname: string, task's cname

        """
        task = self._get_task_by_cname(cname)
        self._delete_task(task)

    def _update_status(self, task_id, next_status,
                       *ensure_previous):

        def __update_status(pipe):
            previous = not_bytes(pipe.hget(metakey, 'status'))
            previous = _loads(previous)
            if previous not in ensure_previous:
                raise TaskStatusNotMatched(
                    'status of task "{0}" is not matched ({1} not in {2})'
                    .format(task_id, previous, ensure_previous))
            pipe.multi()
            now = utcnow()
            pipe.hmset(metakey, {
                'status': _dumps(next_status),
                'last_run_at': _dumps(now.isoformat())
            })
            return now

        metakey = self.__metakey(task_id)
        self.redis.transaction(__update_status, metakey,
                               value_from_callable=True)


class Task(object):

    __slots__ = ('request', 'id', 'uuid', 'cname',
                 '_eta', 'schedule', '_last_run_at', 'status',
                 'on_success', 'on_failure', 'on_complete', '_taskqueue')

    def __init__(self, request, id=None, uuid=None, cname=None,
                 countdown=None, eta=None, schedule=None,
                 last_run_at=None, status='new', on_success=None,
                 on_failure='__report__', on_complete=None):
        self.id = id
        self.request = request
        self.uuid = uuid
        self.cname = cname
        self.countdown = countdown
        if countdown is None:
            self.eta = eta
        self.schedule = schedule
        self.last_run_at = last_run_at
        # valid status: new, scheduled, delayed, running
        self.status = status
        self.on_success = on_success
        self.on_failure = on_failure
        self.on_complete = on_complete
        self._taskqueue = None

    __init_args = inspect.getargspec(__init__).args
    __init_args.pop(0)
    __init_args = set(__init_args)

    def bind_taskqueue(self, tq):
        self._taskqueue = weakref.proxy(tq)

    @property
    def taskqueue(self):
        if self._taskqueue is None:
            raise RuntimeError('task is not bound with taskqueue')
        # ensure the weakref object still exists
        return self._taskqueue.__weakref__

    @property
    def eta(self):
        return self._eta

    @eta.setter
    def eta(self, val):
        if val:
            val = utc.normalize(val)
        self._eta = val

    @property
    def last_run_at(self):
        return self._last_run_at

    @last_run_at.setter
    def last_run_at(self, val):
        if val:
            val = utc.normalize(val)
        self._last_run_at = val

    @property
    def countdown(self):
        if self.eta:
            delta = self.eta - utcnow()
            return get_total_seconds(delta)

    @countdown.setter
    def countdown(self, val):
        if not val:
            # None or 0
            return
        delta = timedelta(seconds=val)
        self.eta = utcnow() + delta

    @staticmethod
    def _schedule_to_string(sched):
        if isinstance(sched, schedules.crontab):
            return ('{0._orig_minute} {0._orig_hour} '
                    '{0._orig_day_of_month} {0._orig_month_of_year} '
                    '{0._orig_day_of_week}').format(sched)
        else:
            return 'every {0.seconds} seconds'.format(sched)

    _sched_pattern = re.compile('every\s*(\d+\.?\d*|\d*\.?\d+)\s*seconds?')

    @classmethod
    def _schedule_from_string(cls, text):
        if cls._sched_pattern.match(text):
            seconds = float(cls._sched_pattern.sub('\g<1>', text))
            return schedules.schedule(seconds)
        else:
            m, h, dom, mon, dow = text.split()
            return schedules.crontab(
                minute=m, hour=h, day_of_week=dow,
                day_of_month=dom, month_of_year=mon)

    @classmethod
    def _wrap_response(cls, response):
        return _dumps({
            'url': response.url,
            'status_code': response.status_code,
            'headers': dict(response.headers),
            'content': not_bytes(response.content),
            'history': [cls._wrap_response(r) for r in response.history],
            'reason': response.reason
        })

    def _report_response(self, response):
        pass

    def _dispatch_callback(self, method, response):
        if method == '__report__':
            return self._report_response(response)

        payload = self._wrap_response(response)
        if isinstance(method, basestring) and \
                method.lower().startswith('http'):
            method = {
                'request': {'method': 'POST',
                            'url': method}
            }
        if isinstance(method, dict):
            # chained task
            kwargs = copy.deepcopy(method)
            if 'headers' not in kwargs['request']:
                kwargs['request']['headers'] = {}
            kwargs['request']['headers'].update({
                'X-Asynx-Chained': self.request['url'],
                'X-Asynx-Chained-TaskUUID': self.uuid,
                'X-Asynx-Chained-TaskETA': (self.eta.isoformat()
                                            if self.eta else '-'),
            })
            if self.cname:
                kwargs['request']['headers'].update({
                    'X-Asynx-Chained-taskCName': self.cname
                })
            kwargs['request']['payload'] = payload
            return self.taskqueue.add_task(**kwargs)

    def apply_async(self, last_run_at):
        tq = self.taskqueue
        args = [tq.__class__, tq.appname, tq.queuename, self.id]
        if self.schedule is not None:
            # scheduled task
            is_due, remaining_s = self.schedule.is_due(last_run_at)
            if is_due:
                # apply immediately, no time to set up status
                result = request_task.apply_async(args)
            else:
                result = request_task.apply_async(
                    args, countdown=remaining_s)
                if remaining_s > 0.5:
                    self.status = 'scheduled'
        elif self.eta is None or self.countdown <= 0:
            # apply async immediately
            result = request_task.apply_async(args)
        else:
            result = request_task.apply_async(
                args, countdown=self.countdown)
            if self.countdown > 0.5:
                self.status = 'delayed'
        self.uuid = result.id
        return result

    def dispatch(self):
        last_run_at = self.taskqueue._update_status(
            self.id, 'running', 'new', 'scheduled', 'delayed')
        self.status = 'running'
        self.last_run_at = last_run_at
        response = self._dispatch(**self.request)
        status_code = response.status_code
        if status_code >= 200 and status_code < 303:
            self._dispatch_callback(self.on_success, response)
        else:
            self._dispatch_callback(self.on_failure, response)
        self._dispatch_callback(self.on_complete, response)
        if self.schedule:
            # schedule next running
            self.taskqueue._dispatch_task(self)
        else:
            # afterward, delete the task whatever
            self.taskqueue._delete_task(self)

    def _dispatch(self, method, url, headers=None,
                  payload=None, timeout=None,
                  allow_redirects=None):
        options = {}
        if headers:
            options['headers'] = headers
        else:
            options['headers'] = headers = {}
        if payload and method in ('POST', 'PUT', 'PATCH'):
            options['data'] = payload
        if timeout is not None:
            options['timeout'] = timeout
        if allow_redirects is not None:
            options['allow_redirects'] = allow_redirects
        elif method in ('GET', 'OPTIONS'):
            options['allow_redirects'] = True
        elif method in ('HEAD', ):
            options['allow_redirects'] = False
        headers.update({
            'X-Asynx-QueueName': self.taskqueue.queuename,
            'X-Asynx-TaskUUID': self.uuid,
            'X-Asynx-TaskETA': self.eta.isoformat() if self.eta else '-',
        })
        headers.setdefault('User-Agent', user_agent())
        if self.cname:
            headers['X-Asynx-TaskCName'] = self.cname
        return requests.request(method, url, **options)

    def to_dict(self):
        return {
            'request': self.request,
            'id': self.id,
            'uuid': self.uuid,
            'cname': self.cname,
            'countdown': self.countdown,
            'eta': self.eta,
            'schedule': self.schedule,
            'last_run_at': self.last_run_at,
            'status': self.status,
            'on_success': self.on_success,
            'on_failure': self.on_failure,
            'on_complete': self.on_complete}

    def _to_redis(self):
        task = self.to_dict()
        task_id = task.pop('id')
        # don't store relative countdown in redis
        task.pop('countdown')
        if task['eta']:
            task['eta'] = task['eta'].isoformat()
        if task['schedule']:
            task['schedule'] = self._schedule_to_string(task['schedule'])
        if task['last_run_at']:
            task['last_run_at'] = task['last_run_at'].isoformat()
        for key, val in dict_items(task):
            task[key] = _dumps(val)
        return task_id, task

    @classmethod
    def from_dict(cls, task_dict):
        task_dict = dict([
            (key, val) for key, val in dict_items(task_dict)
            if key in cls.__init_args])
        return Task(**task_dict)

    @classmethod
    def _from_redis(cls, task_id, task_dict):
        task_dict_tmp = {}
        for key, val in dict_items(task_dict):
            task_dict_tmp[not_bytes(key)] = _loads(not_bytes(val))
        task_dict = task_dict_tmp
        task_dict['id'] = task_id
        if task_dict['eta'] is not None:
            task_dict['eta'] = parser.parse(task_dict['eta'])
        if task_dict['schedule'] is not None:
            task_dict['schedule'] = \
                cls._schedule_from_string(task_dict['schedule'])
        if task_dict['last_run_at'] is not None:
            task_dict['last_run_at'] = parser.parse(task_dict['last_run_at'])
        return cls.from_dict(task_dict)
