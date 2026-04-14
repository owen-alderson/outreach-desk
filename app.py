import streamlit as st
import os
from utils import load_env, generate_email

load_env()

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Outreach Email Generator", page_icon="✉️", layout="centered")

st.markdown("""
<style>
    .output-box {
        background: #1a1d27;
        border: 1px solid #2a2d3d;
        border-radius: 10px;
        padding: 20px 24px;
        font-family: monospace;
        font-size: .9rem;
        line-height: 1.7;
        white-space: pre-wrap;
        color: #e5e7eb;
        margin-top: 8px;
    }
    .section-label {
        font-size: .75rem;
        font-weight: 600;
        color: #6b7280;
        text-transform: uppercase;
        letter-spacing: .06em;
        margin-bottom: 10px;
        margin-top: 24px;
    }
</style>
""", unsafe_allow_html=True)

st.title("✉️ Outreach Email Generator")
st.caption("Fill in the details — get a clean, non-cringe cold email in seconds.")

# ── API key check ─────────────────────────────────────────────────────────────

if not os.environ.get("ANTHROPIC_API_KEY"):
    st.warning("Add `ANTHROPIC_API_KEY=your_key` to a `.env` file in the project folder.")
    st.stop()

# ── Form ──────────────────────────────────────────────────────────────────────

st.markdown('<div class="section-label">About you</div>', unsafe_allow_html=True)
col1, col2 = st.columns(2)
with col1:
    sender_name = st.text_input("Your name", placeholder="Owen Alderson")
with col2:
    sender_context = st.text_input(
        "Your context",
        placeholder="BBA/CS student at IE University, Madrid",
    )

st.markdown('<div class="section-label">Who you\'re writing to</div>', unsafe_allow_html=True)
col3, col4, col5 = st.columns(3)
with col3:
    target_name = st.text_input("Their name", placeholder="Luigi Rizzo")
with col4:
    target_role = st.text_input("Their role", placeholder="Vice Chair, Investment Banking")
with col5:
    target_company = st.text_input("Their company", placeholder="Morgan Stanley")

st.markdown('<div class="section-label">Your ask</div>', unsafe_allow_html=True)

goal = st.selectbox(
    "Goal",
    [
        "Request a 20-minute intro call",
        "Ask for career advice",
        "Apply for an internship or job",
        "Propose a collaboration or project",
        "Request an investor meeting",
        "Ask to be introduced to someone they know",
        "Follow up after meeting in person",
    ],
)

goal_detail = st.text_area(
    "Specifics (optional but recommended)",
    placeholder="e.g. I'm exploring PE roles in London after graduating in 2027 — interested in how you think about the transition from IB to PE",
    height=90,
)

mutual = st.text_input(
    "Mutual connection (optional)",
    placeholder="e.g. Paris de l'Etraz suggested I reach out",
)

tone = st.radio(
    "Tone",
    ["Professional", "Warm and direct", "Confident and brief"],
    horizontal=True,
)

st.divider()

# ── Generate ──────────────────────────────────────────────────────────────────

generate = st.button("Generate Email", type="primary", use_container_width=True)

if generate:
    if not sender_name:
        st.error("Add your name.")
    elif not target_name or not target_role or not target_company:
        st.error("Fill in the target's name, role, and company.")
    else:
        with st.spinner("Writing..."):
            result = generate_email(
                sender_name=sender_name,
                sender_context=sender_context,
                target_name=target_name,
                target_role=target_role,
                target_company=target_company,
                goal=goal,
                goal_detail=goal_detail,
                mutual=mutual,
                tone=tone,
            )
        st.session_state["last_result"] = result

if "last_result" in st.session_state:
    st.markdown('<div class="section-label">Your email</div>', unsafe_allow_html=True)
    result = st.session_state["last_result"]
    st.markdown(f'<div class="output-box">{result}</div>', unsafe_allow_html=True)

    st.text_area(
        "Copy from here",
        value=result,
        height=250,
        label_visibility="collapsed",
        key="copy_area",
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("↺ Regenerate", use_container_width=True):
            with st.spinner("Writing another version..."):
                st.session_state["last_result"] = generate_email(
                    sender_name=sender_name,
                    sender_context=sender_context,
                    target_name=target_name,
                    target_role=target_role,
                    target_company=target_company,
                    goal=goal,
                    goal_detail=goal_detail,
                    mutual=mutual,
                    tone=tone,
                )
            st.rerun()
    with col_b:
        st.download_button(
            "⬇️ Download .txt",
            data=result.encode("utf-8"),
            file_name=f"email-to-{target_name.lower().replace(' ','-')}.txt",
            mime="text/plain",
            use_container_width=True,
        )
