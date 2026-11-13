"""Persistent data and evaluation logic for CQA human study.  SQLite for local tests;
PostgreSQL (Neon) for free public hosting. Do not place a live DB URL in git.
"""
import csv
import hashlib
import hmac
import io
import json
import random
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

OUR_METHOD = 'LUI-UGR'
COMPARATORS = ['PostRAG', 'Qwen2.5-7B-Instruct']
DIMENSIONS = ['overall', 'relevance', 'completeness', 'clarity', 'correctness']
BASE_DIMENSIONS = DIMENSIONS[:4]
CHOICES = {'A', 'TIE', 'B', 'UNABLE'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def open_engine(url):
    if not url or not url.strip():
        raise ValueError('未配置 DATABASE_URL')
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800,
                         connect_args={'timeout': 20} if url.startswith('sqlite:') else {},
                         future=True)


DDL = [
    '''CREATE TABLE IF NOT EXISTS evaluators (
         id TEXT PRIMARY KEY, domain TEXT NOT NULL,
         salt TEXT NOT NULL, code_hash TEXT NOT NULL,
         created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS questions (
         id TEXT PRIMARY KEY, domain TEXT NOT NULL,
         question_text TEXT NOT NULL, reference_text TEXT NOT NULL,
         answer_ours TEXT NOT NULL, answer_post TEXT NOT NULL,
         answer_qwen TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS trials (
         id TEXT PRIMARY KEY,
         evaluator_id TEXT NOT NULL REFERENCES evaluators(id),
         ordinal INTEGER NOT NULL, question_id TEXT NOT NULL REFERENCES questions(id),
         domain TEXT NOT NULL, comparator TEXT NOT NULL,
         method_a TEXT NOT NULL, method_b TEXT NOT NULL,
         question_text TEXT NOT NULL, reference_text TEXT NOT NULL,
         answer_a TEXT NOT NULL, answer_b TEXT NOT NULL,
         is_demo INTEGER NOT NULL DEFAULT 0,
         UNIQUE(evaluator_id, ordinal))''',
    '''CREATE TABLE IF NOT EXISTS responses (
         trial_id TEXT PRIMARY KEY REFERENCES trials(id),
         evaluator_id TEXT NOT NULL REFERENCES evaluators(id),
         score_a INTEGER, score_b INTEGER,
         overall TEXT, relevance TEXT, completeness TEXT, clarity TEXT, correctness TEXT,
         reason TEXT NOT NULL DEFAULT '',
         saved_at TEXT NOT NULL, submitted_at TEXT)''',
    'CREATE INDEX IF NOT EXISTS idx_trials_evaluator ON trials (evaluator_id, ordinal)',
]


def init_db(engine):
    with engine.begin() as con:
        for sql in DDL:
            con.execute(text(sql))


def hash_code(code, salt_hex):
    return hashlib.pbkdf2_hmac('sha256', code.encode('utf8'), bytes.fromhex(salt_hex), 240_000).hex()


def make_invites(engine):
    """Return only NEW credentials; existing codes cannot be recovered."""
    created = []
    with engine.begin() as con:
        for domain in ('Meta', 'RPG'):
            for i in range(1, 4):
                eid = f'{domain.lower()}-{i:02d}'
                found = con.execute(text('SELECT id FROM evaluators WHERE id=:eid'), {'eid':eid}).first()
                if found:
                    continue
                salt = secrets.token_hex(16)
                code = secrets.token_urlsafe(20)
                con.execute(text('''INSERT INTO evaluators (id,domain,salt,code_hash,created_at)
                                    VALUES (:eid,:domain,:salt,:hashed,:created)'''),
                            dict(eid=eid,domain=domain,salt=salt,hashed=hash_code(code,salt),created=now()))
                created.append((eid, code))
    return created


def validate_questions(items):
    if not isinstance(items,list) or not items:
        raise ValueError('题库必须是非空 JSON 数组')
    seen=set()
    for q in items:
        for k in ('id','domain','question','answers'):
            if k not in q: raise ValueError(f'缺少字段 {k}: {q!r}')
        if q['id'] in seen: raise ValueError('重复题目 ID: '+str(q['id']))
        seen.add(q['id'])
        if q['domain'] not in ('Meta','RPG'): raise ValueError('domain 只能是 Meta/RPG')
        if not isinstance(q['question'],str) or not q['question'].strip(): raise ValueError('问题文本为空')
        for method in [OUR_METHOD] + COMPARATORS:
            if not isinstance(q['answers'].get(method),str) or not q['answers'][method].strip():
                raise ValueError(f'缺少生成回答 {method}，问题 {q["id"]}')
    return items


def import_questions(engine, items):
    validate_questions(items)
    with engine.begin() as con:
        # No re-import once any assigned tasks exist; prevents inconsistency.
        if con.execute(text('SELECT 1 FROM trials LIMIT 1')).first():
            raise ValueError('已有随机任务，禁止覆盖题库。请备份并创建新的实验数据库。')
        con.execute(text('DELETE FROM questions'))
        for q in items:
            con.execute(text('''INSERT INTO questions
            (id,domain,question_text,reference_text,answer_ours,answer_post,answer_qwen,is_demo)
            VALUES (:id,:domain,:question_text,:reference_text,:answer_ours,:answer_post,:answer_qwen,:is_demo)'''),
            {'id':str(q['id']), 'domain':q['domain'], 'question_text':q['question'],
             'reference_text':str(q.get('reference','')), 'answer_ours':q['answers'][OUR_METHOD],
             'answer_post':q['answers'][COMPARATORS[0]],
             'answer_qwen':q['answers'][COMPARATORS[1]],
             'is_demo':int(bool(q.get('demo', False)))})
    return len(items)


def assign_trials(engine, seed=None):
    """Generate immutable tasks: two rounds per participant, A/B random independently."""
    assigned={}
    with engine.begin() as con:
        evaluators = con.execute(text('SELECT id,domain FROM evaluators ORDER BY id')).mappings().all()
        for e in evaluators:
            eid=e['id']
            existing = con.execute(text('SELECT COUNT(*) FROM trials WHERE evaluator_id=:eid'), {'eid':eid}).scalar_one()
            if existing:
                assigned[eid] = ('existing',existing)
                continue
            qq = [dict(q) for q in con.execute(text('SELECT * FROM questions WHERE domain=:domain ORDER BY id'),
                                               {'domain':e['domain']}).mappings()]
            if not qq: raise ValueError(f'{e["domain"]} 没有问题，请先导入数据')
            # Each evaluator receives independent and auditable randomized assignments.
            rng=random.Random(f'{seed}:{eid}') if seed is not None else random.SystemRandom()
            first=qq[:]; second=qq[:]
            rng.shuffle(first); rng.shuffle(second)
            if len(qq)>1 and first[-1]['id']==second[0]['id']:
                second=second[1:]+second[:1]
            first_comp={q['id']:rng.choice(COMPARATORS) for q in qq}
            tasks=[(q,first_comp[q['id']]) for q in first]
            tasks += [(q,next(x for x in COMPARATORS if x!=first_comp[q['id']])) for q in second]
            for ordinal, (q, comp) in enumerate(tasks,1):
                a,b=([OUR_METHOD, comp] if rng.randrange(2)==0 else [comp, OUR_METHOD])
                lookup={OUR_METHOD:q['answer_ours'],COMPARATORS[0]:q['answer_post'],COMPARATORS[1]:q['answer_qwen']}
                con.execute(text('''INSERT INTO trials
                  (id,evaluator_id,ordinal,question_id,domain,comparator,method_a,method_b,
                   question_text,reference_text,answer_a,answer_b,is_demo)
                   VALUES (:id,:evaluator_id,:ordinal,:question_id,:domain,:comparator,
                   :method_a,:method_b,:question_text,:reference_text,:answer_a,:answer_b,:is_demo)'''),
                   {'id':str(uuid.uuid4()), 'evaluator_id':eid, 'ordinal':ordinal,'question_id':q['id'],
                    'domain':e['domain'],'comparator':comp,'method_a':a,'method_b':b,
                    'question_text':q['question_text'],'reference_text':q['reference_text'],
                    'answer_a':lookup[a],'answer_b':lookup[b],'is_demo':q['is_demo']})
            assigned[eid]=('created',len(tasks))
    return assigned


def verify_invitation(engine,code):
    """Slow hash verification; don't log or persist the plain invitation."""
    if not isinstance(code,str) or not (8 <= len(code) <= 180):
        return None
    with engine.connect() as con:
        for e in con.execute(text('SELECT id,domain,salt,code_hash FROM evaluators')).mappings():
            hashed=hash_code(code,e['salt'])
            if hmac.compare_digest(hashed,e['code_hash']):
                return {'id':e['id'],'domain':e['domain']}
    return None


def fetch_progress(engine,eid):
    with engine.connect() as con:
        total=con.execute(text('SELECT COUNT(*) FROM trials WHERE evaluator_id=:eid'),{'eid':eid}).scalar_one()
        done=con.execute(text('''SELECT COUNT(*) FROM responses r JOIN trials t ON t.id=r.trial_id
                                    WHERE t.evaluator_id=:eid AND r.submitted_at IS NOT NULL'''),{'eid':eid}).scalar_one()
        row=con.execute(text('''SELECT t.*,r.score_a,r.score_b,r.overall,r.relevance,
                r.completeness,r.clarity,r.correctness,r.reason,r.submitted_at
                FROM trials t LEFT JOIN responses r ON r.trial_id=t.id
                WHERE t.evaluator_id=:eid AND (r.submitted_at IS NULL OR r.trial_id IS NULL)
                ORDER BY t.ordinal LIMIT 1'''),{'eid':eid}).mappings().first()
    return total, done, dict(row) if row else None


def save_first_stage(engine, eid, trial_id, scores, prefs, score_max):
    for k in ('score_a','score_b'):
        v=scores.get(k)
        if not isinstance(v,int) or isinstance(v,bool) or not (0<=v<=score_max):
            raise ValueError(f'{k} 必须是 0–{score_max} 的整数')
    for k in BASE_DIMENSIONS:
        if prefs.get(k) not in CHOICES: raise ValueError('请先完成前四个维度')
    with engine.begin() as con:
        trial=con.execute(text('''SELECT t.id FROM trials t LEFT JOIN responses r ON r.trial_id=t.id
            WHERE t.id=:tid AND t.evaluator_id=:eid AND (r.submitted_at IS NULL OR r.trial_id IS NULL)'''),
            {'tid':trial_id,'eid':eid}).first()
        if not trial: raise ValueError('任务已提交或不属于该评估者，请刷新')
        exists=con.execute(text('SELECT 1 FROM responses WHERE trial_id=:tid'),{'tid':trial_id}).first()
        values=dict(tid=trial_id,eid=eid,score_a=scores['score_a'],score_b=scores['score_b'],
                    overall=prefs['overall'],relevance=prefs['relevance'],completeness=prefs['completeness'],
                    clarity=prefs['clarity'],saved=now())
        if exists:
            con.execute(text('''UPDATE responses SET score_a=:score_a,score_b=:score_b,
                              overall=:overall,relevance=:relevance,completeness=:completeness,
                              clarity=:clarity,saved_at=:saved WHERE trial_id=:tid AND submitted_at IS NULL'''),values)
        else:
            con.execute(text('''INSERT INTO responses
                (trial_id,evaluator_id,score_a,score_b,overall,relevance,completeness,
                clarity,correctness,reason,saved_at,submitted_at)
                VALUES (:tid,:eid,:score_a,:score_b,:overall,:relevance,:completeness,
                :clarity,NULL,'',:saved,NULL)'''),values)


def submit_correctness(engine,eid,trial_id,correctness,reason):
    if correctness not in CHOICES: raise ValueError('请选择正确性评价')
    if not isinstance(reason,str) or len(reason)>3000: raise ValueError('评价理由不能超过3000字')
    with engine.begin() as con:
        res=con.execute(text('''SELECT r.trial_id FROM responses r JOIN trials t ON t.id=r.trial_id
             WHERE t.evaluator_id=:eid AND r.trial_id=:tid AND r.submitted_at IS NULL
             AND r.score_a IS NOT NULL AND r.score_b IS NOT NULL
             AND r.overall IS NOT NULL AND r.relevance IS NOT NULL
             AND r.completeness IS NOT NULL AND r.clarity IS NOT NULL'''), {'eid':eid,'tid':trial_id}).first()
        if not res: raise ValueError('请先完成第一阶段评价，或该任务已经提交')
        con.execute(text('''UPDATE responses SET correctness=:correctness,reason=:reason,
               submitted_at=:ts,saved_at=:ts WHERE trial_id=:tid AND submitted_at IS NULL'''),
            {'correctness':correctness,'reason':reason.strip(),'ts':now(),'tid':trial_id})


EXPORT_FIELDS=['evaluator_id','domain','ordinal','question_id','comparator','method_a','method_b',
    'score_a','score_b','score_ours','score_comparator','score_difference',
    'overall','relevance','completeness','clarity','correctness',
    'preference_overall','preference_relevance','preference_completeness',
    'preference_clarity','preference_correctness','reason','submitted_at','saved_at','is_demo']
REPORT_FIELDS=['domain','comparator','dimension','win','tie','loss','unable',
               'valid_n','total_n','win_rate','tie_rate','loss_rate','valid_raters',
               'valid_questions','mean_score_difference']


def export_rows(engine):
    with engine.connect() as con:
        rows=con.execute(text('''SELECT t.*,r.score_a,r.score_b,r.overall,r.relevance,r.completeness,
                 r.clarity,r.correctness,r.reason,r.submitted_at,r.saved_at
                 FROM trials t JOIN responses r ON r.trial_id=t.id
                 WHERE r.submitted_at IS NOT NULL
                 ORDER BY t.domain,t.evaluator_id,t.ordinal''')).mappings().all()
    out=[]
    for r in rows:
        d={k:r.get(k) for k in EXPORT_FIELDS if k in r}
        ours_a=r['method_a']==OUR_METHOD
        d['score_ours']=r['score_a'] if ours_a else r['score_b']
        d['score_comparator']=r['score_b'] if ours_a else r['score_a']
        d['score_difference']=d['score_ours']-d['score_comparator']
        for dim in DIMENSIONS:
            v=r[dim]
            d['preference_'+dim]=('TIE' if v=='TIE' else 'UNABLE' if v=='UNABLE'
                                  else 'WIN' if (v=='A')==ours_a else 'LOSS')
        out.append(d)
    return out


def make_report(rows):
    out=[]
    for domain in ('Meta','RPG'):
        for comparator in COMPARATORS:
            subset=[r for r in rows if r['domain']==domain and r['comparator']==comparator]
            for dim in DIMENSIONS:
                labels=[r['preference_'+dim] for r in subset]
                w,t,l,u=[labels.count(i) for i in ('WIN','TIE','LOSS','UNABLE')]
                n=w+t+l
                valid=[r for r in subset if r['preference_'+dim] in ('WIN','TIE','LOSS')]
                by_question={}
                if dim=='overall':
                    for r in subset:
                        by_question.setdefault(r['question_id'],[]).append(r['score_difference'])
                averages=[sum(a)/len(a) for a in by_question.values() if a]
                out.append({'domain':domain,'comparator':comparator,'dimension':dim,
                     'win':w,'tie':t,'loss':l,'unable':u,'valid_n':n,'total_n':len(subset),
                     'win_rate':round(w/n,6) if n else '',
                     'tie_rate':round(t/n,6) if n else '',
                     'loss_rate':round(l/n,6) if n else '',
                     'valid_raters':len({r['evaluator_id'] for r in valid}),
                     'valid_questions':len({r['question_id'] for r in valid}),
                     'mean_score_difference':round(sum(averages)/len(averages),6) if averages else ''})
    return out


def csv_text(rows,fields):
    buf=io.StringIO()
    writer=csv.DictWriter(buf,fieldnames=fields,extrasaction='ignore')
    writer.writeheader();writer.writerows(rows)
    return '\ufeff'+buf.getvalue()


def snapshot(engine):
    """All tables, including identity assignments and questions; excludes secrets."""
    out={'snapshot_at':now(),'format_version':1,'tables':{}}
    with engine.connect() as con:
        for tab in ('evaluators','questions','trials','responses'):
            out['tables'][tab]=[dict(r) for r in con.execute(text('SELECT * FROM '+tab)).mappings()]
    return out
