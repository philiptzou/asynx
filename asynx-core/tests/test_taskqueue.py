# -*- coding: utf-8 -*-

from unittest import TestCase
from datetime import datetime, timedelta

import redis
import anyjson
from celery import Celery

from asynx_core.taskqueue import (TaskQueue, Task,
                                  TaskNotFound,
                                  TaskStatusNotMatched,
                                  TaskAlreadyExists)


class TaskQueueTestCase(TestCase):

    def setUp(self):
        self.conn0 = redis.StrictRedis()
        self.conn1 = redis.StrictRedis(db=1)
        self.conn0.delete('celery')
        self.conn1.flushdb()
        self.app = Celery(broker='redis://')

    def tearDown(self):
        self.conn0.delete('celery')
        self.conn1.flushdb()

    def test_dispatch_task(self):
        tq = TaskQueue('test')
        tq.bind_redis(self.conn1)
        task = Task({'method': 'GET',
                     'url': 'http://httpbin.org'},
                    1)
        idx, task_dict = task._to_redis()
        metakey = tq._TaskQueue__metakey(idx)
        self.assertTrue(self.conn1.hmset(metakey, task_dict))
        tq._dispatch_task(task)
        r = self.conn1.hmget(metakey, 'status', 'kind', 'request',
                             'cname', 'on_complete', 'on_failure',
                             'on_success', 'uuid')
        self.assertEqual(r[0:-1], [
            '"enqueued"', '"Task"',
            '{"url": "http://httpbin.org", "method": "GET"}',
            'null', 'null', '"__report__"', '"__delete__"'])
        clobj = anyjson.loads(self.conn0.lindex('celery', 0))
        self.assertEqual(clobj['properties']['correlation_id'],
                         anyjson.loads(r[-1]))
        task = Task({'method': 'GET',
                     'url': 'http://httpbin.org'},
                    2, countdown=10)
        idx, task_dict = task._to_redis()
        metakey = tq._TaskQueue__metakey(idx)
        self.assertTrue(self.conn1.hmset(metakey, task_dict))
        tq._dispatch_task(task)
        self.assertEqual(self.conn1.hget(metakey, 'status'), '"delayed"')

    def test_add_task(self):
        tq = TaskQueue('test')
        tq.bind_redis(self.conn1)
        now = datetime.now()
        delta = timedelta(seconds=2.718287)
        task = tq.add_task(
            {'method': 'GET',
             'url': 'http://httpbin.org'},
            cname='task001',
            eta=now + delta)
        self.assertEqual(task['status'], 'delayed')
        self.assertEqual(task['cname'], 'task001')
        self.assertTrue(2.5 < task['countdown'] < 2.71287)
        self.assertRaises(TaskAlreadyExists,
                          tq.add_task, {}, cname='task001')

    def test_iter_tasks(self):
        tq = TaskQueue('test')
        tq.bind_redis(self.conn1)
        for i in xrange(51):
            tq.add_task(
                {'method': 'GET',
                 'url': 'http://httpbin.org/get'},
                cname='task{0}'.format(2 * i))
            tq.add_task(
                {'method': 'POST',
                 'url': 'http://httpbin.org/post',
                 'payload': 'test'},
                cname='task{0}'.format(2 * i + 1))
        offset93 = tq.iter_tasks(93)
        task93 = offset93.next()
        self.assertEqual(task93['cname'], 'task93')
        j = 0
        for task in tq.iter_tasks(per_pipeline=17):
            self.assertEqual(task['cname'], 'task{0}'.format(j))
            if j % 2:
                self.assertEqual(task['request']['method'], 'POST')
            else:
                self.assertEqual(task['request']['method'], 'GET')
            j += 1
        return tq

    def test_list_tasks(self):
        tq = self.test_iter_tasks()
        tasks = tq.list_tasks(17, 83)
        self.assertEqual(len(tasks), 83)
        for i, task in zip(xrange(17, 100), tasks):
            self.assertEqual(task['cname'], 'task{0}'.format(i))

    def test_get_task(self):
        tq = TaskQueue('test')
        tq.bind_redis(self.conn1)
        for i in xrange(5):
            tq.add_task({'method': 'GET',
                         'url': 'http://httpbin.org'})
        task = tq.get_task(5)
        self.assertEqual(task['status'], 'enqueued')
        self.assertEqual(task['id'], 5)
        self.assertEqual(task['cname'], None)
        self.assertRaises(TaskNotFound, tq.get_task, 6)

    def test_get_task_by_uuid(self):
        tq = TaskQueue('test')
        tq.bind_redis(self.conn1)
        for i in xrange(5):
            task = tq.add_task({'method': 'GET',
                                'url': 'http://httpbin.org'})
        task = tq.get_task_by_uuid(task['uuid'])
        self.assertEqual(task['status'], 'enqueued')
        self.assertEqual(task['id'], 5)
        self.assertEqual(task['cname'], None)
        self.assertRaises(TaskNotFound, tq.get_task_by_uuid, 'notuuid')

    def test_get_task_by_cname(self):
        tq = TaskQueue('test')
        tq.bind_redis(self.conn1)
        task = tq.add_task({'method': 'GET',
                            'url': 'http://httpbin.org'},
                           cname='tasktest')
        task = tq.get_task_by_cname('tasktest')
        self.assertEqual(task['status'], 'enqueued')
        self.assertEqual(task['id'], 1)
        self.assertEqual(task['cname'], 'tasktest')
        self.assertRaises(TaskNotFound, tq.get_task_by_cname, 'notexist')

    def test_delete_task(self):
        conn1 = self.conn1
        tq = TaskQueue('test')
        tq.bind_redis(conn1)
        tq.add_task({'method': 'GET',
                     'url': 'http://httpbin.org'},
                    cname='deletetask')
        tq.delete_task(1)
        self.assertRaises(TaskNotFound, tq.delete_task, 2)
        self.assertFalse(conn1.exists(tq._TaskQueue__metakey(1)))
        self.assertFalse(conn1.exists(tq._TaskQueue__cnamekey('deletetask')))
        self.assertFalse(conn1.exists(tq._TaskQueue__uuidkey()))
        self.assertEqual(conn1.hget(*tq._TaskQueue__hincrkey()), '1')

    def test_delete_task_by_uuid(self):
        conn1 = self.conn1
        tq = TaskQueue('test')
        tq.bind_redis(conn1)
        task = tq.add_task({'method': 'GET',
                            'url': 'http://httpbin.org'})
        tq.delete_task_by_uuid(task['uuid'])
        self.assertRaises(TaskNotFound, tq.delete_task_by_uuid, 'notuuid')
        self.assertFalse(conn1.exists(tq._TaskQueue__metakey(1)))
        self.assertFalse(conn1.exists(tq._TaskQueue__uuidkey()))

    def test_delete_task_by_cname(self):
        conn1 = self.conn1
        tq = TaskQueue('test')
        tq.bind_redis(conn1)
        tq.add_task({'method': 'GET',
                     'url': 'http://httpbin.org'},
                    cname='deletetask')
        tq.delete_task_by_cname('deletetask')
        self.assertFalse(conn1.exists(tq._TaskQueue__metakey(1)))
        self.assertFalse(conn1.exists(tq._TaskQueue__cnamekey('deletetask')))
        self.assertFalse(conn1.exists(tq._TaskQueue__uuidkey()))
        self.assertRaises(TaskNotFound, tq.delete_task_by_uuid, 'notexist')

    def test_update_status(self):
        conn1 = self.conn1
        tq = TaskQueue('test')
        tq.bind_redis(conn1)
        tq.add_task({'method': 'GET',
                     'url': 'http://httpbin.org'},
                    cname='deletetask')
        tq._update_status(1, 'running', 'enqueued', 'delayed')
        self.assertRaises(
            TaskStatusNotMatched,
            tq._update_status,
            1, 'running', 'enqueued', 'delayed')