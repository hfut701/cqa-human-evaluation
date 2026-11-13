"""CQA-RAG human evaluation — publicly shared URL, private invitations and external DB.
Run locally: streamlit run streamlit_app.py
Database setup is deliberately handled by manage.py from the researcher's machine.
"""
import os

import streamlit as st
from dotenv import load_dotenv
load_dotenv()

from core import (open_engine, init_db, verify_invitation, fetch_progress,
                  save_first_stage, submit_correctness)

st.set_page_config(page_title='CQA-RAG 人工评价',page_icon='📋',layout='wide',initial_sidebar_state='expanded')

STYLES = '''
<style>
.block-container {max-width: 1360px; padding-top: 1.8rem; padding-bottom: 5rem;}
div[data-testid="stForm"] {border-radius: 14px; background: #f9fbff; border: 1px solid #dce6f3; padding: 18px;}
[data-testid="stSidebar"] {background: #f6f9fd;}
[data-testid="stTextArea"] textarea {font-size: .96rem; line-height: 1.55;}
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
if score_max not in (7,10):
    st.error('SCORE_MAX 必须设为 7 或 10。')
    st.stop()

@st.cache_resource(show_spinner=False)
def get_engine(address):
    engine=open_engine(address)
    init_db(engine)
    return engine

import logging

try:
    engine = get_engine(url)
except Exception:
    logging.exception("Neon database connection failed")
    st.error("数据库暂时不可用，请联系实验组织者。")
    st.stop()

LABELS={'A':'A 更好','TIE':'两者相当','B':'B 更好','UNABLE':'无法判断'}
PREFERENCE_QUESTIONS=[
    ('overall','Q1 · 整体有用性','哪份回答整体上更能帮助用户满足该问题的信息需求？'),
    ('relevance','Q2 · 相关性','哪份回答更直接回应当前问题的对象、情境和约束？'),
    ('completeness','Q3 · 完整性','哪份回答更充分覆盖解决问题所需的关键点、条件和例外？'),
    ('clarity','Q4 · 易理解性','哪份回答表达更清晰、指代更明确、结构更容易理解？'),
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
    st.write('先对 A/B 分别给出整体有用性评分，再完成前四项成对评价。提交第一阶段后开放参考材料，最后评价正确性。')
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

stage_two=trial['overall'] is not None and trial['score_a'] is not None and trial['score_b'] is not None
st.title('CQA-RAG 人工评估')
st.caption(f'{domain} 数据集 · 第 {trial["ordinal"]} / {total} 次成对比较 · 当前阶段：'+('正确性核查' if stage_two else '独立质量评价'))
if trial.get('is_demo'):
    st.warning('这是演示题目，不能用于正式研究结论。')
with st.container(border=True):
    st.subheader('Target Question · 目标问题')
    st.write(trial['question_text'])

left,right=st.columns(2,gap='medium')
with left:
    with st.container(border=True):
        st.markdown('#### Response A')
        st.text_area('匿名回答 A', value=trial['answer_a'], height=290, disabled=True,
                     label_visibility='collapsed',key=f'answer-a-{trial["id"]}')
with right:
    with st.container(border=True):
        st.markdown('#### Response B')
        st.text_area('匿名回答 B',value=trial['answer_b'],height=290,disabled=True,
                     label_visibility='collapsed',key=f'answer-b-{trial["id"]}')

st.info('请基于问题的信息需求评价两份回答。回答 A、B 的真实来源已匿名化且位置随机。')
if not stage_two:
    with st.form(key=f'first-{trial["id"]}'):
        st.subheader('第一阶段：整体有用性评分与四维成对评价')
        st.write(f'分别为 A 和 B 的整体有用性评分（0–{score_max}，只能选择整数）。')
        col_a,col_b=st.columns(2)
        with col_a:
            score_a=st.radio('A 的整体有用性', list(range(score_max+1)),horizontal=True,index=None)
        with col_b:
            score_b=st.radio('B 的整体有用性', list(range(score_max+1)),horizontal=True,index=None)
        st.divider()
        pref={}
        for key,label,question in PREFERENCE_QUESTIONS:
            st.markdown(f'**{label}**')
            st.write(question)
            pref[key]=st.radio(f'请选择 {label}',options=list(LABELS),
                                format_func=lambda x:LABELS[x],horizontal=True,index=None,
                                key=f'{trial["id"]}-{key}',label_visibility='collapsed')
        next_step=st.form_submit_button('保存第一阶段，进入正确性核查',type='primary',use_container_width=True)
        if next_step:
            if score_a is None or score_b is None or any(v is None for v in pref.values()):
                st.error('请完成 A/B 两项评分及全部四项成对判断。')
            else:
                try:
                    save_first_stage(engine,uid,trial['id'],{'score_a':score_a,'score_b':score_b},pref,score_max)
                    st.rerun()
                except Exception as e:
                    st.error(f'保存失败：{e}')
else:
    with st.container(border=True):
        st.subheader('第一阶段已保存')
        st.write(f'整体有用性评分：A = {trial["score_a"]}，B = {trial["score_b"]}。前四项成对评价已锁定。')
        st.caption('为避免参考材料影响此前判断，第一阶段不能在正确性核查后修改。')
    st.subheader('第二阶段：正确性核查')
    st.write('请核查回答中的核心事实、规则、政策、推理及其必要适用条件。参考答案只是核查线索，并非唯一正确答案。')
    with st.expander('查看本题参考材料与核查提示',expanded=True):
        if trial['reference_text'].strip():
            st.write(trial['reference_text'])
        else:
            st.warning('本题未附参考材料，请根据可靠来源核查；若缺少足够依据请选择“无法判断”。')
        st.caption('注意政策适用时间、游戏系统及版本。与采纳答案不同的解释也可能成立。')
    with st.form(key=f'second-{trial["id"]}'):
        st.markdown('**Q5 · 正确性**')
        st.write('哪份回答的核心事实、规则、政策和推理更正确？')
        correctness=st.radio('正确性评价',list(LABELS),
            format_func=lambda x: LABELS[x],horizontal=True,index=None,label_visibility='collapsed')
        reason=st.text_area('补充理由（可选）',max_chars=3000,
            placeholder='可简述明显错误、关键遗漏、政策版本差异或无法判断的原因。')
        done_btn=st.form_submit_button('提交本题，进入下一题',type='primary',use_container_width=True)
        if done_btn:
            if correctness is None:
                st.error('请先完成 Q5 正确性判断。')
            else:
                try:
                    submit_correctness(engine,uid,trial['id'],correctness,reason)
                    st.rerun()
                except Exception as e:
                    st.error(f'提交失败：{e}')
