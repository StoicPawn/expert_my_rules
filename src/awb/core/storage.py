from __future__ import annotations
import json, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from .models import JobStatus, Task, TaskStatus

SCHEMA='''
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL,created_by TEXT NOT NULL,priority REAL NOT NULL,metadata_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,kind TEXT NOT NULL,task_id TEXT,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gates (gate_id TEXT PRIMARY KEY,passed INTEGER NOT NULL DEFAULT 0,detail TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,status TEXT NOT NULL,requested_minutes REAL NOT NULL,max_steps INTEGER NOT NULL,steps_done INTEGER NOT NULL DEFAULT 0,detail TEXT NOT NULL DEFAULT '',continuous INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY,task_id TEXT NOT NULL,attempt_no INTEGER NOT NULL,status TEXT NOT NULL,route_json TEXT NOT NULL DEFAULT '{}',review_json TEXT NOT NULL DEFAULT '{}',verification_json TEXT NOT NULL DEFAULT '{}',artifact TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',started_at TEXT NOT NULL,finished_at TEXT);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id,attempt_no);
'''
class Ledger:
    def __init__(self,db_path:Path):
        self.db_path=db_path; db_path.parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(str(db_path),timeout=30,check_same_thread=False); self.conn.execute('PRAGMA journal_mode=WAL'); self.conn.execute('PRAGMA busy_timeout=30000'); self.conn.row_factory=sqlite3.Row; self.conn.executescript(SCHEMA); self._migrate(); self.conn.commit()
    def _migrate(self):
        cols={r['name'] for r in self.conn.execute('PRAGMA table_info(jobs)').fetchall()}
        if 'continuous' not in cols: self.conn.execute('ALTER TABLE jobs ADD COLUMN continuous INTEGER NOT NULL DEFAULT 0')
    @staticmethod
    def _now(): return datetime.now(timezone.utc).isoformat()
    def upsert_task(self,task:Task):
        now=self._now(); self.conn.execute('''INSERT INTO tasks(id,title,description,status,created_by,priority,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,description=excluded.description,status=excluded.status,created_by=excluded.created_by,priority=excluded.priority,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at''',(task.id,task.title,task.description,task.status.value,task.created_by,task.priority,json.dumps(task.metadata),now,now)); self.conn.commit()
    def get_task(self,task_id):
        r=self.conn.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone(); return self._row_to_task(r) if r else None
    def list_tasks(self,statuses:Iterable[TaskStatus]|None=None):
        if statuses:
            vals=[s.value for s in statuses]; marks=','.join('?' for _ in vals); rows=self.conn.execute(f'SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY priority DESC,created_at ASC',vals).fetchall()
        else: rows=self.conn.execute('SELECT * FROM tasks ORDER BY priority DESC,created_at ASC').fetchall()
        return [self._row_to_task(r) for r in rows]
    def recover_interrupted_tasks(self):
        rows=self.conn.execute("SELECT id,metadata_json FROM tasks WHERE status='IN_PROGRESS'").fetchall()
        recovered=[]
        for r in rows:
            metadata=json.loads(r['metadata_json']); metadata['interrupted_recovery_count']=int(metadata.get('interrupted_recovery_count',0))+1
            self.conn.execute('UPDATE tasks SET status=?,metadata_json=?,updated_at=? WHERE id=?',(TaskStatus.OPEN.value,json.dumps(metadata),self._now(),r['id']))
            recovered.append(r['id'])
        if recovered:
            self.conn.commit()
            for task_id in recovered: self.event('task_recovered',{'reason':'task was left IN_PROGRESS by an interrupted worker and was reopened'},task_id)
        return recovered
    def event(self,kind,payload,task_id=None): self.conn.execute('INSERT INTO events(ts,kind,task_id,payload_json) VALUES(?,?,?,?)',(self._now(),kind,task_id,json.dumps(payload))); self.conn.commit()
    def set_gate(self,gate_id,passed,detail=''): self.conn.execute('''INSERT INTO gates(gate_id,passed,detail,updated_at) VALUES(?,?,?,?) ON CONFLICT(gate_id) DO UPDATE SET passed=excluded.passed,detail=excluded.detail,updated_at=excluded.updated_at''',(gate_id,int(passed),detail,self._now())); self.conn.commit()
    def gate_state(self): return {r['gate_id']:{'passed':bool(r['passed']),'detail':r['detail']} for r in self.conn.execute('SELECT * FROM gates').fetchall()}
    def start_attempt(self,task_id:str,attempt_no:int):
        aid=f'ATT-{uuid.uuid4().hex[:10].upper()}'; self.conn.execute('INSERT INTO attempts(id,task_id,attempt_no,status,started_at) VALUES(?,?,?,?,?)',(aid,task_id,attempt_no,'RUNNING',self._now())); self.conn.commit(); return aid
    def finish_attempt(self,attempt_id:str,*,status:str,route=None,review=None,verification=None,artifact:str='',error:str=''):
        self.conn.execute('''UPDATE attempts SET status=?,route_json=?,review_json=?,verification_json=?,artifact=?,error=?,finished_at=? WHERE id=?''',(status,json.dumps(route or {}),json.dumps(review or {}),json.dumps(verification or {}),artifact,error,self._now(),attempt_id)); self.conn.commit()
    def list_attempts(self,task_id:str|None=None,limit:int=100):
        if task_id:
            rows=self.conn.execute('SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT ?',(task_id,limit)).fetchall()
        else:
            rows=self.conn.execute('SELECT * FROM attempts ORDER BY started_at DESC LIMIT ?',(limit,)).fetchall()
        out=[]
        for r in rows:
            item=dict(r)
            for key in ('route_json','review_json','verification_json'):
                item[key[:-5]]=json.loads(item.pop(key) or '{}')
            out.append(item)
        return out
    def create_job(self,requested_minutes:float,max_steps:int,continuous:bool=False):
        jid=f'JOB-{uuid.uuid4().hex[:10].upper()}'; now=self._now(); self.conn.execute('INSERT INTO jobs(id,status,requested_minutes,max_steps,steps_done,detail,continuous,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(jid,JobStatus.QUEUED.value,requested_minutes,max_steps,0,'queued',int(continuous),now,now)); self.conn.commit(); return jid
    def update_job(self,job_id,*,status=None,steps_done=None,detail=None):
        cur=self.get_job(job_id)
        if not cur: raise KeyError(job_id)
        self.conn.execute('UPDATE jobs SET status=?,steps_done=?,detail=?,updated_at=? WHERE id=?',((status or JobStatus(cur['status'])).value,cur['steps_done'] if steps_done is None else steps_done,cur['detail'] if detail is None else detail,self._now(),job_id)); self.conn.commit()
    def get_job(self,jid):
        r=self.conn.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone(); return dict(r) if r else None
    def latest_job(self):
        r=self.conn.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1').fetchone(); return dict(r) if r else None
    def list_jobs(self,limit=20): return [dict(r) for r in self.conn.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?',(limit,)).fetchall()]
    def recoverable_jobs(self): return [dict(r) for r in self.conn.execute("SELECT * FROM jobs WHERE continuous=1 AND status IN ('RUNNING','QUEUED') ORDER BY created_at").fetchall()]
    def recent_events(self,limit=50): return [{'seq':r['seq'],'ts':r['ts'],'kind':r['kind'],'task_id':r['task_id'],'payload':json.loads(r['payload_json'])} for r in self.conn.execute('SELECT * FROM events ORDER BY seq DESC LIMIT ?',(limit,)).fetchall()]
    @staticmethod
    def _row_to_task(r): return Task(id=r['id'],title=r['title'],description=r['description'],status=TaskStatus(r['status']),created_by=r['created_by'],priority=r['priority'],metadata=json.loads(r['metadata_json']))
