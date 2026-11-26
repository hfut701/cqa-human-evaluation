"""CQA-RAG human evaluation — publicly shared URL, private invitations and external DB.
Run locally: streamlit run streamlit_app.py
Database setup is deliberately handled by manage.py from the researcher's machine.
"""
import os

import streamlit as st
from dotenv import load_dotenv
load_dotenv()

from core import (open_engine, init_db, verify_invitation, fetch_progress,
                  submit_evaluation)

st.set_page_config(page_title='CQA-RAG 人工评价',page_icon='📋',layout='wide',initial_sidebar_state='expanded')

STYLES = '''
<style>
/* 统一深色文字和高对比度学术阅读界面。 */
[data-testid="stAppViewContainer"] {background-color: #F4F7FB;}
.block-container {max-width: 1500px; padding-top: 1.6rem; padding-bottom: 5rem;}
[data-testid="stSidebar"] {background: #EDF2F9;}
[data-testid="stMarkdownContainer"] {color: #17283F;}
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li {color: #23354E; line-height: 1.72;}
[data-testid="stMarkdownContainer"] a {color: #1557A8; text-decoration: underline;}
[data-testid="stVerticalBlockBorderWrapper"] {
    background: #FFFFFF;
    border-color: #B8C8DD !important;
    border-radius: 12px;
}
div[data-testid="stForm"] {
    background: #FFFFFF;
    border: 1px solid #B8C8DD;
    border-radius: 14px;
    box-shadow: 0 3px 12px rgba(26, 47, 77, .045);
    padding: 1.25rem 1.5rem;
}
/* 增强阅读区文字和标题的识别度，防止以往 disabled 文本框的发灰问题。 */
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] {color:#182A43;}
[data-testid="stRadio"] label {color: #203451 !important;}
[data-testid="stWidgetLabel"] {color: #203451; font-weight: 600;}
.score-scale-legend {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: .8rem;
    max-width: 465px;
    margin: -.35rem 0 .75rem 0;
    font-size: .81rem;
    color: #405773;
}
.score-scale-legend span:last-child {text-align: right;}
.score-scale-legend strong {color: #213C61;}
@media(max-width:720px) {
    .block-container {padding-left: 1rem; padding-right: 1rem;}
    .score-scale-legend {max-width:100%; font-size:.75rem;}
}
</style>'''
st.markdown(STYLES,unsafe_allow_html=True)

# Streamlit Cloud Secrets take precedence over process environment variables.
def secret_value(key, fallback=None):
    try:
        return st.secrets.get(key, os.getenv(key, fallback))
    except Exception:
        return os.getenv(key, fallback)

url=secret_value('DATABASE_URL')
if not url and secret_value('LOCAL_DEMO')=='1':
    url='sqlite:///local_development.sqlite3'
if not url:
    st.error('管理员尚未配置持久化数据库 DATABASE_URL。为防止评价数据丢失，本应用不会在云端使用临时 SQLite。')
    st.stop()

score_max=int(secret_value('SCORE_MAX','7'))
if score_max != 7:
    st.error('本版实验统一使用 1–7 分，请将 SCORE_MAX 设置为 7。')
    st.stop()

@st.cache_resource(show_spinner=False)
def get_engine(address):
    engine=open_engine(address)
    init_db(engine)
    return engine

try:
    engine=get_engine(url)
except Exception:
    st.error('数据库暂时不可用，请联系实验组织者。')
    st.stop()

LABELS={'A':'A 更好','TIE':'两者相当','B':'B 更好','UNABLE':'无法判断'}
PREFERENCE_QUESTIONS=[
    ('overall','Q1 · 整体有用性','哪份回答整体上更能帮助用户满足该问题的信息需求？'),
    ('relevance','Q2 · 相关性','哪份回答更直接回应当前问题的对象、情境和约束？'),
    ('completeness','Q3 · 完整性','哪份回答更充分覆盖解决问题所需的关键点、条件和例外？'),
    ('clarity','Q4 · 易理解性','哪份回答表达更清晰、指代更明确、结构更容易理解？'),
    ('correctness','Q5 · 正确性','哪份回答的核心事实、规则、政策和推理更正确？'),
]

# Never show comparator names, author method, or assignment mapping to evaluator.
def clear_user():
    st.session_state.pop('evaluation_user',None)

user=st.session_state.get('evaluation_user')
if not user:
    st.title('CQA-RAG · 人工评估')
    st.caption('匿名回答成对比较 · Meta Stack Exchange / RPG Stack Exchange')
    with st.container(border=True):
        st.subheader('评估者登录')
        st.write('请输入实验组织者发送给您的独立邀请码。同一人可使用自己的邀请码多次进入，续评进度将保存至数据库。')
        with st.form('login_form'):
            code=st.text_input('邀请码', type='password', placeholder='请输入邀请码')
            ok=st.form_submit_button('进入人工评价',type='primary')
        if ok:
            try:
                evaluator=verify_invitation(engine,code.strip())
            except Exception:
                st.error('登录服务暂时不可用，请联系实验组织者。')
                st.stop()
            if evaluator:
                st.session_state['evaluation_user']=evaluator
                st.rerun()
            else:
                st.error('邀请码无效，请确认是否复制完整。')
    st.info('本实验不要求您提供姓名或邮箱。请独立完成各维度判断，不要根据回答长度、语气或对模型来源的猜测选择答案。')
    st.stop()

uid=user['id']; domain=user['domain']
with st.sidebar:
    st.title('评价任务')
    st.caption(f'评估者：{uid}')
    st.caption(f'数据集：{domain}')
    if st.button('退出登录',use_container_width=True):
        clear_user();st.rerun()
    st.divider()
    st.markdown('**评价说明**')
    st.write('每题只需提交一次：先分别为 A/B 的整体有用性评分，再完成五个维度的成对比较。')
    st.caption('“两者相当”表示可以判断但差异不明显；“无法判断”表示缺少可靠依据，两者会被分别记录。')

try:
    total, done, trial=fetch_progress(engine,uid)
except Exception:
    st.error('读取评估进度失败，评价记录未更改。请稍后重新登录。')
    st.stop()
with st.sidebar:
    st.progress(done/total if total else 0)
    st.write(f'已完成 **{done} / {total}** 个比较任务')

if total==0:
    st.error('此邀请码尚未分配任务，请管理员先运行 python manage.py assign。')
    st.stop()
if trial is None:
    st.title('全部评价已完成')
    st.success(f'您已完成 {total} 次成对比较，非常感谢您的参与。')
    st.write('所有评价结果已经保存到实验数据库。可以关闭浏览器。')
    st.stop()

st.title('CQA-RAG 人工评估')
st.caption(f'{domain} 数据集 · 第 {trial["ordinal"]} / {total} 次成对比较')
if trial.get('is_demo'):
    st.warning('这是演示题目，不能用于正式研究结论。')
with st.container(border=True):
    st.subheader('Target Question · 目标问题')
    st.write(trial['question_text'])

# Long answers are rendered as readable Markdown rather than disabled grey textareas.
# In standard mode both responses have equal-height scrollable reading areas;
# evaluators can expand them fully if desired.
expand_answers = st.toggle('展开 A/B 完整回答（不限制高度）', value=False,
                           key=f'expand-answers-{trial["id"]}')
reading_height = None if expand_answers else 560
left, right = st.columns(2, gap='large')
with left:
    st.markdown('#### Response A')
    with st.container(height=reading_height, border=True):
        st.markdown(trial['answer_a'] or '（此回答为空）')
with right:
    st.markdown('#### Response B')
    with st.container(height=reading_height, border=True):
        st.markdown(trial['answer_b'] or '（此回答为空）')

st.info('请根据当前问题和回答内容独立判断。正确性指核心事实、规则、政策和推理是否成立，并非与历史采纳答案是否一致。A/B 的来源已匿名化且展示位置随机。')

# All six ratings are on a single page, using a single atomic submission.
# Saved, unsubmitted first-stage values from an older version are prefilled.
if trial.get('score_a') is not None and trial.get('score_b') is not None:
    st.caption('本题存在之前保存但尚未提交的评分；已为您自动填入，可在最终提交前修改。')

with st.form(key=f'evaluation-{trial["id"]}'):
    st.subheader('整体有用性评分')
    st.write('请根据当前问题的信息需求，综合判断回答在多大程度上能够为用户提供有用的信息，并分别对回答 A 和 B 进行1–7分评价。')
    a_col, b_col = st.columns(2, gap='large')
    scale = list(range(1, 8))  # Never expose score 0.
    saved_a, saved_b = trial.get('score_a'), trial.get('score_b')
    with a_col:
        score_a = st.radio('回答 A 的整体有用性', scale, horizontal=True,
                           index=scale.index(saved_a) if saved_a in scale else None,
                           key=f'score-a-{trial["id"]}')
        st.markdown('''<div class="score-scale-legend">
            <span><strong>1 分</strong> · 几乎无法满足信息需求</span>
            <span><strong>7 分</strong> · 充分满足信息需求</span>
        </div>''', unsafe_allow_html=True)
    with b_col:
        score_b = st.radio('回答 B 的整体有用性', scale, horizontal=True,
                           index=scale.index(saved_b) if saved_b in scale else None,
                           key=f'score-b-{trial["id"]}')
        st.markdown('''<div class="score-scale-legend">
            <span><strong>1 分</strong> · 几乎无法满足信息需求</span>
            <span><strong>7 分</strong> · 充分满足信息需求</span>
        </div>''', unsafe_allow_html=True)

    st.divider()
    st.subheader('五个维度的成对评价')
    st.caption('“两者相当”表示能够判断，但该维度没有明显差异；“无法判断”表示缺乏足够知识或证据。两个选项分别记录。')
    preferences = {}
    options = list(LABELS)
    for key, label, question in PREFERENCE_QUESTIONS:
        st.markdown(f'**{label}**')
        st.write(question)
        saved = trial.get(key)
        preferences[key] = st.radio(
            f'请选择 {label}', options=options,
            format_func=lambda value: LABELS[value], horizontal=True,
            index=options.index(saved) if saved in options else None,
            key=f'{trial["id"]}-{key}', label_visibility='collapsed',
        )
        if key == 'correctness':
            st.caption('依据回答自身的事实、规则和逻辑判断。无需与历史采纳答案一致；若无法可靠判断，请选择“无法判断”。')

    reason = st.text_area('补充理由（可选）', value=trial.get('reason') or '',
                          max_chars=3000,
                          placeholder='例如明显的事实错误、关键遗漏、适用条件、或无法判断的原因。')
    submit = st.form_submit_button('提交本题，进入下一题', type='primary', use_container_width=True)
    if submit:
        if score_a is None or score_b is None or any(v is None for v in preferences.values()):
            st.error('请完成 A/B 的整体有用性评分，以及五个维度的全部成对判断。')
        else:
            try:
                submit_evaluation(engine, uid, trial['id'],
                                  {'score_a': score_a, 'score_b': score_b},
                                  preferences, reason, score_max)
                st.rerun()
            except Exception:
                # Avoid exposing database internals or connection strings to visitors.
                st.error('提交失败，评价记录未更改。请刷新后重试或联系实验组织者。')
