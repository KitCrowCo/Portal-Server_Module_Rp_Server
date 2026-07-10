import os, uuid, json, pathlib, datetime, re
from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from typing import Optional

from modules.rp_server.models import *

MODULE_META = {"label": "RP Server", "icon": "&#x1F4AC;", "description": "Collaborative roleplay, TTRPG, and chat rooms", "persistence": "user"}

router = APIRouter()

RP_ASSET_URL = "/module_assets/rp_server"
RP_ASSET_DIR = pathlib.Path("./data/module_assets/rp_server")

IM = None
TM = None
UI = None
md_plus_transpiler = None
tab_bar_from_state = None
IMResponse = None
ENV = {"auth": None, "templates": None, "get_state": None, "set_state": None, "tools": {}, "send_push": None, "InterfaceManager": None, "IMResponse": None, "push_to_client": None}

async def _render_active_tab_html(request, state: dict = None):
    """Called by TM to produce content area HTML on tab changes."""
    state.setdefault("tabs", {})
    state.setdefault("active", None)
    active = state.get("active")
    if not active or active not in state["tabs"]: return #state, _LAUNCHER_HTML
    path = state["tabs"][active].get("path", "")
    if not path: return #state, _LAUNCHER_HTML
    html = "<div></div>" #await _render_file_page(request, path, state)
    return state, html  #.body.decode()

# This will be expanded for level 0 and PWA handling.
def init_module(environment: dict):
    global ENV, IM, TM, UI, md_plus_transpiler, tab_bar_from_state, IMResponse
    ENV.update(environment)
    UI = ENV.get("templates").env.globals.get("UI")
    md_plus_transpiler = ENV["tools"]["built_ins"].md_plus_transpiler
    tab_bar_from_state = ENV["tools"]["built_ins"].tab_bar_from_state
    IMResponse = ENV["IMResponse"]
    IM = ENV["InterfaceManager"](nesting_level=1, db_path="rp_server_im.db")
    #TM = ENV["tools"]["built_ins"].TabManager(namespace = "rp_server", tab_bar_id = "rp_server-tab-bar", content_id = "rp_server_content", render_content_fn = _render_active_tab_html, intent_prefix = "rp_server", IM = IM)
    print("RP Server: environment loaded.")
    
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# --- User resolution ---
# Always from request.state.user — set by inject_context middleware, safe under concurrent requests.
# Never from Jinja globals which are shared across all concurrent requests.

def _user(request: Request): return getattr(request.state, "user", None)
def _username(request: Request) -> str: u = _user(request); return u.username if u else "guest"
def _role(request: Request) -> str: u = _user(request); return getattr(u, "role", "user")

# --- Module state ---
# rp_server keeps session state in its own UserSession table (module DB).
# These wrappers exist for any callers that use them; portal state API is not used for room/persona state.

async def rp_save_state(state: dict, request: Request = None):
    await ENV["set_state"](request, state, scope="session", namespace="rp_server")

async def rp_get_state(request: Request = None) -> dict:
    state = await ENV["get_state"](request, scope="session", namespace="rp_server")
    state.setdefault("current_room", "Public")
    state.setdefault("current_persona", "")
    return state

# --- DB helpers ---

def get_user_session(db: Session, username: str) -> UserSession:
    sess = db.get(UserSession, username)
    if sess is None:
        sess = UserSession(username=username, current_room="Public", current_persona=username)
        db.add(sess); db.commit(); db.refresh(sess)
    return sess

def save_info(obj, updates: dict, db: Session):
    obj.info = {**(obj.info or {}), **updates}
    flag_modified(obj, "info")
    db.add(obj); db.commit()

def ensure_public_room(db: Session):
    if not db.get(Room, "Public"):
        db.add(Room(id="Public", owner="admin", room_type="public", title="Public")); db.commit()

def can_access_room(db: Session, room: Room, username: str, role: str) -> bool:
    if room.room_type == "public": return True
    if role == "admin": return True
    if room.owner == username: return True
    return db.query(RoomMembership).filter(RoomMembership.room_id == room.id, RoomMembership.username == username).first() is not None

def room_label(room: Room) -> str: return room.title or room.id
def safe_room_id(raw: str) -> str: return re.sub(r"[^a-zA-Z0-9_-]", "", raw.strip())[:64]

# --- WS broadcast ---
# ws is the ConnectionManager instance keyed by user.username strings.
# Injected via ENV["tools"]["ws"] — add Tools["ws"] = manager in main.py before load_modules.
async def broadcast_to_room(db: Session, room_id: str, payload: dict):
    ws = ENV.get("tools", {}).get("ws")
    if not ws: return
    room = db.get(Room, room_id)
    if not room: return
#    msg = {"type": "rp_message", "payload": payload}
    msg = {"t": "trigger", "event": "rp_message", "detail": payload}
    if room.room_type == "public":
        await ws.broadcast(msg); return
    members = {r.username for r in db.query(RoomMembership).filter(RoomMembership.room_id == room_id)}
    active  = {s.username for s in db.query(UserSession).filter(UserSession.current_room == room_id)}
    for uid in members | active: await ws.send_personal_message(msg, uid)
    
# --- UI helpers ---
# These fill the gap for UI methods referenced in the original code that don't exist in style.py yet.

def _ui_field(label: str, input_html: str) -> str:
    return f'<div style="display:flex;flex-direction:column;gap:0.2rem;margin-bottom:0.6rem;"><label style="font-size:0.75rem;color:var(--text_muted);">{label}</label>{input_html}</div>'

def _ui_input(name: str, value: str = "", placeholder: str = "", type_: str = "text", extra: str = "") -> str:
    return f'<input name="{name}" type="{type_}" value="{value}" placeholder="{placeholder}" {extra} style="background:var(--bg); border:var(--border-thick) solid var(--border); color:var(--text); padding:0.4rem 0.6rem; border-radius:var(--radius);width:100%; box-sizing:border-box;">'

def _ui_textarea(name: str, value: str = "", rows: int = 3, placeholder: str = "") -> str:
    return f'<textarea name="{name}" rows="{rows}" placeholder="{placeholder}" style="background:var(--bg);var(--border-thick) solid var(--border); color:var(--text);padding:0.4rem 0.6rem;border-radius:var(--radius);width:100%;box-sizing:border-box;font-family:var(--font-main);resize:vertical;">{value}</textarea>'

def _ui_select(name: str, options: list, selected: str = "") -> str:
    opts = "".join(f'<option value="{UI.escape(v)}" {"selected" if v == selected else ""}>{UI.escape(l)}</option>' for v, l in options)
    return f'<select name="{name}" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:0.4rem;border-radius:var(--radius);width:100%;">{opts}</select>'

def _ui_collapsible(title: str, content: str, open_: bool = False) -> str:
    return f'<details {"open" if open_ else ""} style="margin-bottom:0.8rem;"><summary style="font-size:0.8rem;font-weight:600;color:var(--text_muted);cursor:pointer;padding:0.3rem 0;">{title}</summary><div style="padding:0.4rem 0;">{content}</div></details>'

def _ui_modal(content: str, title: str = "") -> str:
    heading = f'<h3 style="margin-top:0; color:var(--accent);">{title}</h3>' if title else ""
    return f"""<div class="glass rp-ui-modal">
				   <button style="position:absolute; top:0.5rem; right:0.5rem; background:none; border:none; cursor:pointer; font-size:1rem; color:var(--text_muted);" onclick="document.getElementById('rp-modal').innerHTML=''">&#x2715;</button>
                   {heading}{content}
               </div>"""

# --- CSS ---

RP_CSS = """
/* RP Server Navigation Variables */
:root {
    --rp-item-gap: 0.25rem;
    --rp-avatar-size: 1.6rem;
    --rp-sidebar-padding: 0.5rem;
}

.flex-col {display: flex !important; flex-direction: column !important; height: 100% !important;}

#rp-messages {flex: 1 1 0% !important; display: block !important; overflow-y: auto !important; min-height: 5rem; scroll-behavior: smooth;}

/* Prevent sidebar sections from collapsing */
.rp-section-label {
    flex-shrink: 0;
    padding: 0.5rem 0.2rem 0.2rem 0.2rem;
    font-size: 0.75rem;
    font-weight: bold;
    text-transform: uppercase;
    opacity: 0.7;
}

.fixed-shrink {flex-shrink: 0 !important;}
#rp-msg-input {field-sizing: content; flex:1;background:var(--bg); color:var(--text);border:var(--border-thick) solid var(--border);border-radius:var(--radius);padding:0.4rem;font-family:var(--font-main);font-size:var(--font-size);resize:none;overflow-y:auto;max-height:25vh;line-height:1.4;}

.rp-msg:hover
.rp-msg-actions {display: flex;}
.rp-msg-btn {background:var(--surface);border:var(--border-thick) solid var(--border);color:var(--text_muted); border-radius:0.3rem; padding:0.15rem 0.35rem; cursor:pointer; font-size:0.7rem; line-height:1;}
.rp-msg-actions {display:none; gap:0.2rem; margin-bottom:0.2rem;}
.rp-msg-btn:hover {color:var(--accent); border-color:var(--accent);}
.rp-bubble {word-break:break-word;}

.rp-meta {user-select:none;}
.rp-room-item {display:flex;align-items:center;justify-content:space-between;padding:0.3rem 0.5rem;border-radius:0.4rem;cursor:pointer; }
.rp-room-item:hover
.rp-room-item.active {background:var(--accent_dim);color:var(--accent);}

.feedback-btn.htmx-settling::after { content:' \u2714';color:var(--accent); }
.persona-list {display: flex; flex-direction: column; gap: var(--rp-item-gap); padding: var(--rp-sidebar-padding);}
.rp-ui-modal {padding: 1.5rem; min-width: 20rem; max-width: 90vw; max-height: 85vh; position: relative; border-radius: var(--radius);}

/* Layout Modules - Essential for Toolbars */
.notflex-col { display: flex; flex-direction: column; height: 100%; width: 100%; overflow: hidden; }
.flex-row { display: flex; gap: 0.5rem; align-items: center; }
.scroll-y { flex: 1; overflow-y: auto; scroll-behavior: smooth; }
.notfixed-shrink { flex-shrink: 0; }

/* Persona Grid - Essential for the Menu */
.persona-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(6rem, 1fr)); gap: 0.8rem; padding: 0.5rem 0; }
.persona-card { 
    display: flex; flex-direction: column; align-items: center; justify-content: center; 
    padding: 0.5rem; border-radius: var(--radius); border: var(--border-thick) solid var(--border);
    cursor: pointer; position: relative; background: var(--surface);
}
.persona-card.active { border-color: var(--accent); background: var(--accent_dim); }
.persona-card img, .p-init { width: 4rem; height: 4rem; border-radius: 1rem; object-fit: cover; display: flex; align-items: center; justify-content: center;color-interpolation: sRGB; }

/* Input Row for Toolbars */
.input-row { display: flex; gap: 0.4rem; align-items: center; width: 100%; }
.toolbar-header { padding: 0.4rem 0.6rem; border-bottom: var(--border-thick) solid var(--border); display: flex; justify-content: space-between; align-items: center; }
"""

# --- Script ---
# Minimal JS. Message receipt via WS (rp_message event from IB dispatch) replaces polling.
# Poll div kept as fallback at 8s interval.

RP_SCRIPT = """
document.addEventListener('input', function(e) { if (e.target.id === 'rp-msg-input') { e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, window.innerHeight * 0.25) + 'px'; } });
let rpEditing = false, rpLastMsgId = 0;
document.body.addEventListener('htmx:afterRequest', e => { if (e.detail.elt?.id !== 'rp-msg-form') return; const inp = document.getElementById('rp-msg-input'); if (inp) { inp.value = ''; inp.style.height = 'auto'; } rpScrollBottom(true); });
document.body.addEventListener('htmx:configRequest', e => { if (e.detail.elt?.id === 'rp-messages' && rpEditing) e.preventDefault(); });
document.body.addEventListener('htmx:afterSwap', e => { if (e.detail.target?.id !== 'rp-messages') return; const last = e.detail.target.querySelector('.rp-msg:last-child'); if (last?.dataset.msgId) rpLastMsgId = parseInt(last.dataset.msgId); const isLoad = e.detail.requestConfig?.triggerSpecs?.some(t => t.trigger === 'load'); rpScrollBottom(isLoad); });
document.body.addEventListener('newMessages', e => { const poll = document.getElementById('rp-poll'); if (poll) poll.setAttribute('hx-vals', JSON.stringify({last_id: e.detail?.value || rpLastMsgId})); });
document.addEventListener('rp_message', function() { document.body.dispatchEvent(new CustomEvent('messagePosted', {bubbles: true})); rpScrollBottom(false); });
function rpScrollBottom(force) { const m = document.getElementById('rp-messages'); if (!m) return; if (force || (m.scrollHeight - m.scrollTop - m.clientHeight < 150)) m.scrollTop = m.scrollHeight; }

if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
"""

# --- Bubble renderer ---

_ME_BG = "rgba(0,160,220,0.18)"; _ME_BORD = "rgba(0,180,255,0.45)"
_OTH_BG = "rgba(0,180,100,0.15)"; _OTH_BORD = "rgba(0,200,120,0.40)"

def bubble_html(m, my_persona: str, username: str = "", room_owner: str = "", sprites: dict = None) -> str:
    sprites  = sprites or {}
    persona  = UI.escape(m.persona_name or m.user_name or "?")
    t        = getattr(m, "story_time", None) or m.created_at
    ts       = t.strftime("%H:%M") if t else ""
    content  = md_plus_transpiler(m.content or "")
    is_me    = (m.persona_name == my_persona)
    can_edit = (m.user_name == username)
    can_del  = can_edit or (room_owner == username)
    bg, bord, align = (_ME_BG, _ME_BORD, "flex-end") if is_me else (_OTH_BG, _OTH_BORD, "flex-start")
    sprite   = sprites.get(m.persona_name or "")
    #avatar   = (f"""<img src='{UI.escape(sprite)}' style='width:1.8rem;height:1.8rem;border-radius:50%;object-fit:cover;flex-shrink:0;border:1px solid var(--border); color-interpolation: sRGB;'>""" if sprite else f"""<div style='width:1.8rem;height:1.8rem;border-radius:50%;background:var(--accent_dim);border:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:0.7rem;font-weight:600;color:var(--accent);'>{UI.escape((m.persona_name or '?')[0].upper())}</div>""")
    # Add 'filter: none !important;' and 'color-scheme: light;'
    avatar = (f"""<img src='{UI.escape(sprite)}' 
    style='width:1.8rem;height:1.8rem;border-radius:50%;object-fit:cover;flex-shrink:0;
    border:var(--border-thick) solid var(--border); color-interpolation: sRGB; 
    filter: none !important; color-scheme: light;'>""" 
    if sprite else f"""<div style='width:1.8rem;height:1.8rem;border-radius:50%;background:var(--accent_dim);border:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:0.7rem;font-weight:600;color:var(--accent);'>{UI.escape((m.persona_name or '?')[0].upper())}</div>""")
    
    edit_btn = (f"<button class='rp-msg-btn' hx-get='/module/rp_server/messages/edit_modal?msg_id={m.id}' hx-target='#rp-modal' hx-swap='innerHTML' onclick='rpEditing=true'>&#x270E;</button>") if can_edit else ""
    del_btn  = (f"<button class='rp-msg-btn' hx-post='/module/rp_server/messages/delete' hx-vals='{{\"msg_id\":\"{m.id}\"}}' hx-swap='none'>&#x2715;</button>") if can_del else ""
    actions  = f"<div class='rp-msg-actions'>{edit_btn}{del_btn}</div>" if (can_edit or can_del) else ""
    inner    = f"<div style='display:flex;flex-direction:column;align-items:{'flex-end' if is_me else 'flex-start'};max-width:88%;'>{actions}<div id='rp-bubble-{m.id}' class='rp-bubble' style='padding:0.55rem 0.85rem;border-radius:1rem;background:{bg};border:1px solid {bord};'><div class='rp-meta' style='font-size:0.7rem;opacity:0.65;margin-bottom:0.2rem;'>{persona} &middot; {ts}</div><div class='rp-content'>{content}</div></div></div>"
    return f"<div class='rp-msg' data-msg-id='{m.id}' style='display:flex;justify-content:{align};align-items:flex-start;gap:0.4rem;margin-bottom:0.6rem;position:relative;'>{''+avatar if not is_me else ''}{inner}{''+avatar if is_me else ''}</div>"

# --- Main page ---

@router.get("/", response_class=HTMLResponse)
async def ui_index(request: Request, db: Session = Depends(get_db)):
    user         = _user(request)
    username     = _username(request)
    role         = _role(request)
    ensure_public_room(db)
    sess         = get_user_session(db, username)
    room_id      = sess.current_room or "Public"
    persona      = sess.current_persona or username
    room_obj     = db.get(Room, room_id)
    room_display = UI.escape(room_label(room_obj)) if room_obj else UI.escape(room_id)
    lvl          = int(request.headers.get("x-shell-level", 1))    
    
    TabBarContent = "<div></div>" #tab_bar_from_state()
    
    LeftBarContent = f"""
<div style="display: block; height: 100%; overflow: hidden;">
    
    <div style="height: 60vh; display: flex; flex-direction: column; padding: 0.4rem; box-sizing: border-box; overflow: hidden;">
        <div class="fixed-shrink">
            <div class="rp-section-label">Rooms</div>
            <div id="rp-rooms" class="scroll-y" style="max-height: 25vh; overflow-y: auto; border-bottom:var(--border-thick) solid var(--border);" 
                 hx-get="/module/rp_server/rooms" hx-trigger="load, rpRoomsUpdate from:body" hx-swap="innerHTML">
            </div>
            <div style="padding: 0.5rem 0;">
                <form hx-post="/module/rp_server/rooms/create" class="input-row">
                    <input name="title" class="module-select" placeholder="Room name..." style="flex:1;">
                    <button class="ui-btn">+</button>
                </form>
            </div>
            <div class="rp-section-label">Personas</div>
        </div>

        <div id="rp-personas" style="flex: 1; overflow-y: auto; min-height: 0;" 
             hx-get="/module/rp_server/personas" hx-trigger="load, rpPersonasUpdate from:body" hx-swap="innerHTML">
        </div>
    </div>

    <div style="height: 40vh; padding: 0.4rem; border-top: var(--border-thick) solid var(--border); display: flex; flex-direction: column; gap: 0.3rem; justify-content: center;">
        <button class="ui-btn" hx-get="/module/rp_server/options" hx-target="#rp-modal">Options</button>
        <button class="ui-btn" hx-get="/module/rp_server/room/manage" hx-target="#rp-modal">Room Settings</button>
        <button class="ui-btn" hx-get="/module/rp_server/persona/manage" hx-target="#rp-modal">Persona Settings</button>
    </div>

</div>
"""
    
    RightBarContent = f"""<div style="display: flex; flex-direction: column; height: 100%; width: 100%;">
    <div class="toolbar-header fixed-shrink">
        <span id="rp-room-label" style="font-weight:700; color:var(--accent);" hx-get="/module/rp_server/room/label" hx-trigger="load, rpRoomLabelUpdate from:body">{room_display}</span>
        <span style="font-size:0.75rem; opacity:0.6;">as <b id="rp-persona-label" hx-get="/module/rp_server/persona/label" hx-trigger="load">{UI.escape(persona)}</b></span>
    </div> 

    <div id="rp-messages" class="padding-sm" style="flex: 1; overflow-y: auto; min-height: 100px;" 
         hx-get="/module/rp_server/messages/latest" hx-trigger="load, newMessages from:body, messagePosted from:body" hx-target="this" hx-swap="innerHTML">
    </div>

    <div class="fixed-shrink" style="display:flex;justify-content:flex-end;padding:0.1rem 0.4rem;border-top:var(--border-thick) solid var(--border);">
        <button class="btn-icon" onclick="var m=document.getElementById('rp-messages');if(m)m.scrollTop=m.scrollHeight;" style="font-size:0.9rem;opacity:0.5;padding:0.1rem 0.4rem;">&#x25BC; latest</button>
    </div>

    <div class="fixed-shrink" style="border-top:var(--border-thick) solid var(--border);padding:0.4rem;">
        <form id="rp-msg-form" class="input-row" style="align-items:flex-end; display: flex; gap: 0.4rem;" hx-post="/module/rp_server/messages/send" hx-include="this" hx-swap="none"> 
            <div id="rp-bridge-container" hx-get="/module/rp_server/room/bridge" hx-trigger="load, rpRoomLabelUpdate from:body" hx-target="this" hx-swap="innerHTML">
                <input type="hidden" name="room_id" id="rp-room-bridge" value="{UI.escape(room_id)}">
            </div>
            <textarea id="rp-msg-input" name="content" placeholder="Say something&#x2026;" style="flex: 1;"></textarea>
            <button class="ui-btn" style="flex-shrink:0;">Send</button>
        </form>
    </div>                                
</div>"""
  
    content = f"""<div id="rp-stage" hx-get="/module/rp_server/stage" hx-trigger="load, rpStageUpdate from:body" hx-target="this" hx-swap="innerHTML" style="height:100%;width:100%;background-size:cover;background-position:center;display:flex;align-items:center;justify-content:center;"></div><div id="rp-modal" hx-on:closeModal="this.innerHTML=''" style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2000;"></div>"""

    return ENV["templates"].TemplateResponse("base.html", {
        "request":      request,
        "user":         user,
        "nesting_level": lvl,
        "toolbars": {"top": UI.toolbar(side="top", content=TabBarContent, size="2rem", overlay=False, nesting_level=lvl),
            		 "left": UI.toolbar(side="left", content=LeftBarContent, size="20rem", overlay=True, nesting_level=lvl),
                     "right": UI.toolbar(side="right", content=RightBarContent, size="20rem", overlay=True, start_open=True, resizable=True, nesting_level=lvl)},
        "content":      content,
        "extra_css":    RP_CSS,
        "extra_script": RP_SCRIPT, "shell_id": IM.branch_id})
                                      
# --- Rooms ---

@router.get("/rooms", response_class=HTMLResponse)
async def rooms_list(request: Request, db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    sess     = get_user_session(db, username)
    active   = sess.current_room
    html = ""
    for r in db.query(Room).all():
        if not can_access_room(db, r, username, role): continue
        type_badge = f'<small style="opacity:0.5;font-size:0.7rem;">({UI.escape(r.room_type)})</small>'
        html += f"""<div class="rp-room-item {"active" if r.id == active else ""}" hx-post="/module/rp_server/rooms/join" hx-vals='{{"room_id":"{UI.escape(r.id)}"}}' hx-swap="none">
        				<span>{UI.escape(room_label(r))} {type_badge}</span>
                        <small style="opacity:0.5;">{UI.escape(r.owner or "")}</small>
                    </div>"""
    return HTMLResponse(html)

@router.post("/rooms/create", response_class=HTMLResponse)
async def rooms_create(request: Request, title: str = Form(...), db: Session = Depends(get_db)):
    username = _username(request)
    title    = title.strip()[:120]
    if not title: return HTMLResponse("Title required.", status_code=400)
    room_id = re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-"))[:64] or uuid.uuid4().hex[:12]
    base_id = room_id; counter = 1
    while db.get(Room, room_id): room_id = f"{base_id}-{counter}"; counter += 1
    db.add(Room(id=room_id, owner=username, room_type="private", title=title)); db.commit()
    sess = get_user_session(db, username)
    sess.current_room = room_id; db.add(sess); db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"rpRoomsUpdate": True, "rpStageUpdate": True, "messagePosted": True, "rpRoomLabelUpdate": True})})

@router.post("/rooms/join", response_class=HTMLResponse)
async def rooms_join(request: Request, room_id: str = Form(...), db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    room = db.get(Room, room_id)
    if not room: return HTMLResponse("Room not found.", status_code=404)
    if not can_access_room(db, room, username, role): return HTMLResponse("Access denied.", status_code=403)
    sess = get_user_session(db, username)
    sess.current_room = room_id; db.add(sess); db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"rpRoomsUpdate": True, "rpStageUpdate": True, "messagePosted": True, "rpRoomLabelUpdate": True})})

@router.get("/room/label", response_class=HTMLResponse)
async def room_label_fragment(request: Request, db: Session = Depends(get_db)):
    username = _username(request)
    sess     = get_user_session(db, username)
    room     = db.get(Room, sess.current_room or "Public")
    return HTMLResponse(UI.escape(room_label(room)) if room else UI.escape(sess.current_room or "Public"))

@router.get("/room/current_label", response_class=HTMLResponse)
async def room_current_label(request: Request, db: Session = Depends(get_db)):
    return await room_label_fragment(request, db)

# --- Personas ---

@router.get("/personas", response_class=HTMLResponse)
async def list_personas(request: Request, db: Session = Depends(get_db)):
    username = _username(request)
    sess     = get_user_session(db, username)
    active   = sess.current_persona
    personas = db.query(Persona).filter(Persona.owner_username == username).all()
    sprites  = {p.name: p.sprite for p in personas if p.sprite}
    items    = [{"id": "__username__", "name": username, "is_active": (active == username or not active)}]
    for p in personas: items.append({"id": p.id, "name": p.name, "is_active": (active == p.name)})
    html = ""
    for item in items:
        vals       = UI.escape(json.dumps({"persona_id": str(item["id"])}))
        avatar     = (f'<img src="{UI.escape(sprites.get(item["name"],""))}" style="width:1.4rem;height:1.4rem;border-radius:50%;object-fit:cover;flex-shrink:0;" />' if sprites.get(item["name"]) else f'<div style="width:1.4rem;height:1.4rem;border-radius:50%;background:var(--accent_dim);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:0.6rem;color:var(--accent);">{UI.escape(item["name"][0].upper())}</div>')
        manage_btn = (f'<button class="btn-icon" style="flex-shrink:0;font-size:0.7rem;" hx-get="/module/rp_server/personas/manage/{item["id"]}" hx-target="#rp-modal" hx-swap="innerHTML">&#x270E;</button>' if item["id"] != "__username__" else "")
        html += f'<div class="rp-persona-item {"active" if item["is_active"] else ""}" style="cursor:pointer;"><div style="display:flex;align-items:center;gap:0.4rem;flex:1;min-width:0;" hx-post="/module/rp_server/personas/select" hx-vals=\'{vals}\' hx-swap="none">{avatar}<span style="font-weight:{"600" if item["is_active"] else "400"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{UI.escape(item["name"])}</span></div>{manage_btn}</div>'
    return HTMLResponse(html)

@router.post("/personas/create", response_class=HTMLResponse)
async def create_persona(request: Request, name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    username = _username(request); name = name.strip()[:80]
    if not name: return HTMLResponse("Name required.", status_code=400)
    if db.query(Persona).filter(Persona.owner_username == username, Persona.name == name).first():
        return HTMLResponse(f"Persona '{UI.escape(name)}' already exists.", status_code=400)
    db.add(Persona(name=name, description=description.strip(), owner_username=username)); db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "rpPersonasUpdate"})

@router.post("/personas/select", response_class=HTMLResponse)
async def select_persona(request: Request, persona_id: str = Form(...), db: Session = Depends(get_db)):
    username = _username(request)
    if persona_id == "__username__":
        display = username
    else:
        p = db.get(Persona, int(persona_id))
        if not p or p.owner_username != username: return HTMLResponse("Not found.", status_code=404)
        display = p.name
    sess = get_user_session(db, username)
    sess.current_persona = display; db.add(sess); db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"rpPersonasUpdate": True, "rpPersonaLabelUpdate": True})})

@router.post("/personas/delete", response_class=HTMLResponse)
async def delete_persona(request: Request, persona_id: int = Form(...), db: Session = Depends(get_db)):
    username = _username(request)
    p = db.get(Persona, persona_id)
    if not p or p.owner_username != username: return HTMLResponse("Not found.", status_code=404)
    db.delete(p); db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "rpPersonasUpdate"})

# --- Persona management ---

def persona_management_menu(username: str, personas: list, active_name: str, edit_id = None):
    """Compact polymorphic UI for Persona management."""
    # Gallery Section
    gallery = ""
    for i,p in enumerate([{"id":0, "name":username, "sprite":None}] + [{"id":p.id, "name":p.name, "sprite":p.sprite} for p in personas]):
        is_act = "active" if p["name"] == active_name else ""
        img = f'<img src="{p["sprite"]}">' if p["sprite"] else f'<div class="p-init">{p["name"][0].upper()}</div>'
        gallery += f'''<div class="persona-card {is_act}" hx-post="/module/rp_server/personas/select" hx-vals='{{"persona_id":"{p["id"] or "__username__"}"}}' hx-swap="none">
                            {img}<span>{p["name"]}</span>
                            <div class="btn-icon" style="position:absolute;top:-5px; right:-5px;" hx-get="/module/rp_server/persona/manage?edit={p["id"]}" hx-target="#rp-modal">{"&#x2699;" if p["id"] else ""}</div>
                        </div>'''
        
        #if p["name"]==edit_id or p["id"]==edit_id: p2=personas[i+1]
    # Editor Section
    #if edit_id:
    p2 = next(x for x in personas if x.id == edit_id)
    editor = f'''
    <div style="display:flex;align-items:center;gap:1rem;margin-bottom:1rem;">
    <form hx-post="/module/rp_server/personas/upload_sprite" hx-encoding="multipart/form-data" hx-target="#rp-modal" hx-swap="innerHTML" style="display:flex;flex-direction:column;gap:0.3rem;">
        <input type="hidden" name="persona_id" value="{p2.id}">
        <label style="font-size:0.75rem;opacity:0.7;">Avatar image</label>
        <input type="file" name="sprite" accept="image/*" style="font-size:0.75rem;">
        <button class="ui-btn" style="font-size:0.8rem;">Upload</button>
    </form>
    </div>
    
    
    <form class="edit-form" hx-post="/module/rp_server/personas/update" hx-swap="none">
                            <input type="hidden" name="persona_id" value="{p2.id}">
                            <div class="input-row">
                                {_ui_input("name", p2.name, "Name")}
                                <label class="input-row"><input type="checkbox" name="ai_enabled" value="1" {"checked" if p2.ai_enabled else ""}> AI</label>
                            </div>
                            {_ui_textarea("description", p2.description or "", 2, "Description...")}
                            <div class="p-row">
                                <button class="ui-btn" style="flex:1">Save</button>
                                <button class="ui-btn" style="color:red" hx-post="/module/rp_server/personas/delete" hx-vals='{{"persona_id":{p2.id}}}' hx-confirm="Delete?">Drop</button>
                            </div>
                        </form>'''

    # Mini Creation Footer
    creator = f'''<form class="input-row" hx-post="/module/rp_server/personas/create">
                        <input name="name" class="module-select" placeholder="New Persona..." style="flex:1">
                        <button class="ui-btn">+</button>
                    </form>'''
    return _ui_modal(f'<div class="rp-persona-mgr"><div class="persona-grid">{gallery}</div><div>{editor}</div><div>{creator}</duv></div>', "Personas")

#<form hx-post="/module/rp_server/personas/update" hx-swap="none" style="display:flex;flex-direction:column;gap:0.6rem;"><input type="hidden" name="persona_id" value="{p.id}" />{_ui_field("Name", _ui_input("name", value=UI.escape(p.name)))}{_ui_field("Description", _ui_textarea("description", value=UI.escape(p.description or ""), rows=3))}{sheet_section}{ai_section}<button class="ui-btn" style="margin-top:0.5rem;color:var(--accent);">Save Changes</button></form><div style="margin-top:1rem;padding-top:0.8rem;border-top:1px solid var(--border);"><button class="ui-btn" style="color:#ff6b6b;border-color:#ff6b6b;font-size:0.8rem;width:100%;" hx-post="/module/rp_server/personas/delete" hx-vals=\'{{"persona_id":"{p.id}"}}\' hx-confirm="Delete this persona? This cannot be undone." hx-swap="none" hx-on::after-request="document.getElementById(\'rp-modal\').innerHTML=\'\'">Delete Persona</button></div>')
    
@router.get("/personas/manage/{persona_id}", response_class=HTMLResponse)
async def persona_manage(request: Request, persona_id:int, db: Session = Depends(get_db)):
    username = _username(request)
    p = db.get(Persona, persona_id)
    if not p or p.owner_username != username: return HTMLResponse("Not found.", status_code=404)
    #sheet = p.sheet or {}
    #{sheet_section}{ai_section}
    #sheet_rows = "".join(f'<div style="display:flex;gap:0.3rem;align-items:center;margin-bottom:0.3rem;"><input name="sheet_key" value="{UI.escape(k)}" style="width:8rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:0.3rem;padding:0.2rem 0.4rem;font-size:0.8rem;" /><input name="sheet_val" value="{UI.escape(str(v))}" style="flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:0.3rem;padding:0.2rem 0.4rem;font-size:0.8rem;" /><button type="button" class="rp-msg-btn" onclick="this.closest(\'div\').remove()">&#x2715;</button></div>' for k, v in sheet.items())
    sprite_preview = (f'<img src="{UI.escape(p.sprite)}" style="width:4rem;height:4rem;border-radius:50%;object-fit:cover;border:2px solid var(--accent);" />' if p.sprite else f'<div style="width:4rem;height:4rem;border-radius:50%;background:var(--accent_dim);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:1.5rem;color:var(--accent);">{UI.escape(p.name[0].upper())}</div>')
    #add_field_js = "var d=document.getElementById('sheet-fields');var r=document.createElement('div');r.style='display:flex;gap:0.3rem;align-items:center;margin-bottom:0.3rem;';r.innerHTML='<input name=sheet_key placeholder=Field style=\"width:8rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:0.3rem;padding:0.2rem 0.4rem;font-size:0.8rem;\"><input name=sheet_val placeholder=Value style=\"flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:0.3rem;padding:0.2rem 0.4rem;font-size:0.8rem;\"><button type=button class=rp-msg-btn onclick=\"this.closest(div).remove()\">&#x2715;</button>';d.appendChild(r);"
    sheet_section = _ui_collapsible(f"Character Sheet ({len(sheet)} fields)", f"<div id='sheet-fields'>{sheet_rows}</div><button type='button' class='ui-btn' style='font-size:0.75rem;margin-top:0.3rem;' onclick=\"{add_field_js}\">+ Add field</button>")
    #ai_section = "" #_ui_collapsible("AI Participation (optional)", f"<div style='font-size:0.75rem;opacity:0.6;margin:0.4rem 0;'>Uses only locally-hosted AI on your server. No external services.</div><label style='display:flex;align-items:center;gap:0.5rem;font-size:0.8rem;cursor:pointer;margin-bottom:0.4rem;'><input type='checkbox' name='ai_enabled' value='1' {'checked' if p.ai_enabled else ''} /> Enable AI participation</label>{_ui_field('AI Instructions', _ui_textarea('ai_instructions', value=UI.escape(p.ai_instructions or ''), rows=4, placeholder='Character voice, rules, limits...'))}")
    inner = (f'<div style="display:flex;align-items:center;gap:1rem;margin-bottom:1rem;">{sprite_preview}<form hx-post="/module/rp_server/personas/upload_sprite" hx-encoding="multipart/form-data" hx-target="#rp-modal" hx-swap="innerHTML" style="display:flex;flex-direction:column;gap:0.3rem;"><input type="hidden" name="persona_id" value="{p.id}"><label style="font-size:0.75rem;opacity:0.7;">Avatar image</label><input type="file" name="sprite" accept="image/*" style="font-size:0.75rem;"><button class="ui-btn" style="font-size:0.8rem;">Upload</button></form></div><form hx-post="/module/rp_server/personas/update" hx-swap="none" style="display:flex;flex-direction:column;gap:0.6rem;"><input type="hidden" name="persona_id" value="{p.id}" />{_ui_field("Name", _ui_input("name", value=UI.escape(p.name)))}{_ui_field("Description", _ui_textarea("description", value=UI.escape(p.description or ""), rows=3))}<button class="ui-btn" style="margin-top:0.5rem;color:var(--accent);">Save Changes</button></form><div style="margin-top:1rem;padding-top:0.8rem;border-top:1px solid var(--border);"><button class="ui-btn" style="color:#ff6b6b;border-color:#ff6b6b;font-size:0.8rem;width:100%;" hx-post="/module/rp_server/personas/delete" hx-vals=\'{{"persona_id":"{p.id}"}}\' hx-confirm="Delete this persona? This cannot be undone." hx-swap="none" hx-on::after-request="document.getElementById(\'rp-modal\').innerHTML=\'\'">Delete Persona</button></div>')
    return HTMLResponse(_ui_modal(inner, title=f"Persona: {UI.escape(p.name)}"))

@router.get("/persona/manage", response_class=HTMLResponse)
async def persona_manage_unified(request: Request, edit = None, db: Session = Depends(get_db)):
    """The route called by 'Persona Settings' button."""
    username = _username(request)
    sess = get_user_session(db, username)
    personas = db.query(Persona).filter(Persona.owner_username == username).all()
    persona = sess.current_persona
    if not edit: edit = db.query(Persona).filter(Persona.name == persona).first().id
    return HTMLResponse(persona_management_menu(username, personas, sess.current_persona, edit))

@router.post("/personas/update", response_class=HTMLResponse)
async def persona_update(request: Request, persona_id: int = Form(...), name: str = Form(...), description: str = Form(""), ai_enabled: Optional[str] = Form(None), ai_instructions: str = Form(""), db: Session = Depends(get_db)):
    username = _username(request)
    p = db.get(Persona, persona_id)
    if not p or p.owner_username != username: return HTMLResponse("Forbidden.", status_code=403)
    form_data = await request.form()
    keys = form_data.getlist("sheet_key"); vals = form_data.getlist("sheet_val")
    p.name = name.strip()[:80]; p.description = description.strip(); p.ai_enabled = (ai_enabled == "1"); p.ai_instructions = ai_instructions.strip()
    p.sheet = {k.strip(): v.strip() for k, v in zip(keys, vals) if k.strip()}
    db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "rpPersonasUpdate"})

@router.post("/personas/upload_sprite", response_class=HTMLResponse)
async def upload_sprite(request: Request, persona_id: int = Form(...), sprite: UploadFile = File(...), db: Session = Depends(get_db)):
    username = _username(request)
    p = db.get(Persona, persona_id)
    if not p or p.owner_username != username: return HTMLResponse("Forbidden.", status_code=403)
    ext = pathlib.Path(sprite.filename).suffix.lower()
    if ext not in (".png",".jpg",".jpeg",".webp",".gif"): return HTMLResponse("Invalid file type.", status_code=400)
    dest_dir = RP_ASSET_DIR / username / "sprites"; dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    with open(dest_dir / fname, "wb") as f: f.write(await sprite.read())
    p.sprite = f"{RP_ASSET_URL}/{username}/sprites/{fname}"; db.commit()
    return await persona_manage(request, persona_id, db)

# --- Stage ---

@router.get("/stage", response_class=HTMLResponse)
async def get_stage(request: Request, db: Session = Depends(get_db)):
    username = _username(request)
    sess     = db.get(UserSession, username)
    room_id  = (sess.current_room if sess else None) or "Public"
    room     = db.get(Room, room_id)
    bg       = room.background if room else ""
    bg_val   = f"url({UI.escape(bg)})" if bg else "none"
#    return HTMLResponse(f'<script>var s=document.getElementById("rp-stage");if(s)s.style.backgroundImage="{bg_val}";</script>')
    return HTMLResponse(f"""<div style="height:100%;display:flex;align-items:center;justify-content:center;padding:1rem;"><img src="{bg}" style="max-width:100%;max-height:100%;object-fit:contain;"></div>""")


# --- Messages ---

@router.get("/messages/latest", response_class=HTMLResponse)
async def get_latest_messages(request: Request, limit: int = 120, db: Session = Depends(get_db)):
    username   = _username(request)
    sess       = db.get(UserSession, username)
    room_id    = (sess.current_room if sess else None) or "Public"
    my_persona = (sess.current_persona if sess else None) or username
    messages   = db.query(Message).filter(Message.room_id == room_id, Message.deleted == False).order_by(Message.created_at.asc()).limit(limit).all()
    room       = db.get(Room, room_id)
    room_owner = room.owner if room else ""
    persona_names = {m.persona_name for m in messages if m.persona_name}
    sprites = {p.name: p.sprite for p in db.query(Persona).filter(Persona.name.in_(persona_names)).all() if p.sprite} if persona_names else {}
    html = "".join(bubble_html(m, my_persona, username, room_owner, sprites) for m in messages)
    return HTMLResponse(html or '<div style="opacity:0.4;padding:1rem;text-align:center;">No messages yet.</div>')

@router.post("/messages/send", response_class=HTMLResponse)
async def send_message(request: Request, room_id: str = Form(...), content: str = Form(...), db: Session = Depends(get_db)):
    username = _username(request); content = content.strip()
    if not content: return HTMLResponse("", status_code=204)
    room = db.get(Room, room_id)
    if not room: return HTMLResponse("Room not found.", status_code=404)
    sess    = get_user_session(db, username)
    persona = sess.current_persona or username
    msg     = Message(room_id=room_id, user_name=username, persona_name=persona, content=content)
    db.add(msg); db.commit(); db.refresh(msg)
    await broadcast_to_room(db, room_id, {"room": room_id, "user": username, "persona": persona, "preview": content[:80]})
    send_push = ENV.get("send_push")
    if send_push:
        for w in db.query(RoomWatch).filter(RoomWatch.room_id == room_id, RoomWatch.notify == True).all():
            if w.username == username: continue
            wsess = db.get(UserSession, w.username)
            if wsess and wsess.current_room == room_id: continue
            await send_push(w.username, f"New in {room_label(room)}", f"{persona}: {content[:80]}", db)
    return HTMLResponse("", headers={"HX-Trigger": "messagePosted"})

@router.get("/messages/poll", response_class=HTMLResponse)
async def messages_poll(request: Request, last_id: int = 0, db: Session = Depends(get_db)):
    username  = _username(request)
    sess      = db.get(UserSession, username)
    room_id   = (sess.current_room if sess else None) or "Public"
    latest    = db.query(Message.id).filter(Message.room_id == room_id, Message.deleted == False).order_by(Message.id.desc()).first()
    latest_id = latest[0] if latest else 0
    if latest_id > last_id: return HTMLResponse("", headers={"HX-Trigger": json.dumps({"newMessages": latest_id})})
    return HTMLResponse("", status_code=204)

@router.post("/messages/delete", response_class=HTMLResponse)
async def message_delete(request: Request, msg_id: int = Form(...), db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    msg = db.get(Message, msg_id)
    if not msg: return HTMLResponse("", status_code=204)
    room       = db.get(Room, msg.room_id)
    can_delete = (msg.user_name == username or (room and room.owner == username) or role in ("admin", "superadmin"))
    if not can_delete: return HTMLResponse("Forbidden", status_code=403)
    msg.deleted = True; db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "messagePosted"})

@router.get("/messages/edit_modal", response_class=HTMLResponse)
async def message_edit_modal(request: Request, msg_id: int, db: Session = Depends(get_db)):
    username = _username(request)
    msg      = db.get(Message, msg_id)
    if not msg or msg.user_name != username: return HTMLResponse("Forbidden.", status_code=403)
    personas     = db.query(Persona).filter(Persona.owner_username == username).all()
    persona_opts = [(username, f"{username} (you)")] + [(p.name, p.name) for p in personas]
    t            = getattr(msg, "story_time", None) or msg.created_at
    story_time_val = t.strftime("%Y-%m-%dT%H:%M") if t else ""
    inner = f"<form hx-post='/module/rp_server/messages/edit' hx-swap='none' style='display:flex;flex-direction:column;gap:0.6rem;'><input type='hidden' name='msg_id' value='{msg_id}' />{_ui_field('Persona', _ui_select('persona_name', persona_opts, selected=msg.persona_name or ''))}{_ui_field('Story time', _ui_input('story_time', value=story_time_val, type_='datetime-local'))}{_ui_field('Message', _ui_textarea('content', value=UI.escape(msg.content or ''), rows=6))}<div style='display:flex;gap:0.5rem;justify-content:flex-end;'><button class='ui-btn' style='color:var(--accent);'>Save</button><button type='button' class='ui-btn' onclick=\"document.getElementById('rp-modal').innerHTML=''\">Cancel</button></div></form>"
    return HTMLResponse(_ui_modal(inner, title="Edit Message"))

@router.post("/messages/edit", response_class=HTMLResponse)
async def message_edit(request: Request, msg_id: int = Form(...), content: str = Form(...), persona_name: str = Form(""), story_time: str = Form(""), db: Session = Depends(get_db)):
    username = _username(request)
    msg = db.get(Message, msg_id)
    if not msg or msg.user_name != username: return HTMLResponse("Forbidden.", status_code=403)
    msg.content   = content.strip()
    msg.edited_at = datetime.datetime.utcnow()
    if persona_name.strip(): msg.persona_name = persona_name.strip()
    if story_time.strip():
        try: msg.story_time = datetime.datetime.fromisoformat(story_time)
        except ValueError: pass
    db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"messagePosted": True})})

@router.post("/messages/insert", response_class=HTMLResponse)
async def message_insert(request: Request, after_id: int = Form(0), content: str = Form(...), persona_name: str = Form(""), story_time: str = Form(""), db: Session = Depends(get_db)):
    username = _username(request)
    sess     = get_user_session(db, username)
    room_id  = sess.current_room or "Public"
    persona  = persona_name.strip() or sess.current_persona or username
    st = None
    if story_time.strip():
        try: st = datetime.datetime.fromisoformat(story_time)
        except ValueError: pass
    db.add(Message(room_id=room_id, user_name=username, persona_name=persona, content=content.strip(), story_time=st)); db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"messagePosted": True})})

# --- Room management ---

@router.get("/room/manage", response_class=HTMLResponse)
async def manage_room(request: Request, db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    sess     = get_user_session(db, username)
    room_id  = sess.current_room or "Public"
    room     = db.get(Room, room_id)
    if not room: return HTMLResponse("Room not found.", status_code=404)
    if room.owner != username and role not in ("admin", "superadmin"):
        return HTMLResponse('<div style="padding:1rem;opacity:0.6;">You do not manage this room.</div>')
    members = db.query(RoomMembership).filter(RoomMembership.room_id == room_id).all()
    member_html = "".join(f'<div style="display:flex;justify-content:space-between;padding:0.2rem 0;"><span>{UI.escape(m.username)}</span><button class="ui-btn" style="padding:0.1rem 0.4rem;font-size:0.75rem;" hx-post="/module/rp_server/room/member/remove" hx-vals=\'{{"room_id":"{UI.escape(room_id)}","username":"{UI.escape(m.username)}"}}\' hx-target="#rp-member-list" hx-swap="innerHTML">&#x2715;</button></div>' for m in members) or '<div style="opacity:0.5;font-size:0.8rem;">No members yet.</div>'
    room_info = room.info or {}
    mode_form = f"<form hx-post='/module/rp_server/room/update_info' hx-swap='none' style='display:flex;flex-direction:column;gap:0.6rem;'><input type='hidden' name='room_id' value='{UI.escape(room_id)}' />{_ui_field('Mode', _ui_select('mode', [('general','General Chat'),('otome','Visual Novel'),('ttrpg','TTRPG / D&D')], selected=room_info.get('mode','general')))}{_ui_field('Setting / World', _ui_input('world', value=UI.escape(room_info.get('world','')), placeholder='e.g. Faerun, Original'))}{_ui_field('Story Notes', _ui_textarea('world_detail', value=UI.escape(room_info.get('world_detail','')), rows=4, placeholder='Canon details, current arc...'))}<button class='ui-btn' style='color:var(--accent);'>Save</button></form>"
    bg_line = f'<div style="font-size:0.75rem;opacity:0.5;margin-top:0.3rem;">Current: {UI.escape(room.background)}</div>' if room.background else ""
    inner = (f"<div style='margin-bottom:1rem;'><label style='font-size:0.8rem;opacity:0.7;'>Room Title</label><form hx-post='/module/rp_server/room/update' hx-include='this' hx-swap='none' style='display:flex;gap:0.4rem;margin-top:0.2rem;'><input type='hidden' name='room_id' value='{UI.escape(room_id)}' /><input name='title' value='{UI.escape(room.title or room_id)}' class='module-select' style='flex:1;background:var(--bg);' /><button class='ui-btn'>Save</button></form></div><div style='margin-bottom:1rem;'><label style='font-size:0.8rem;opacity:0.7;'>Background Image</label><form hx-post='/module/rp_server/room/upload_bg' hx-encoding='multipart/form-data' hx-swap='none' style='display:flex;flex-direction:column;gap:0.4rem;margin-top:0.2rem;'><input type='hidden' name='room_id' value='{UI.escape(room_id)}' /><input type='file' name='bgfile' accept='image/*' style='font-size:0.8rem;' /><button class='ui-btn'>Upload</button></form>{bg_line}</div>{_ui_collapsible('Story Mode & Setting', mode_form, open_=True)}<div style='margin-bottom:1rem;'><label style='font-size:0.8rem;opacity:0.7;'>Members</label><div id='rp-member-list' style='margin-top:0.3rem;'>{member_html}</div><form hx-post='/module/rp_server/room/member/add' hx-include='this' hx-target='#rp-member-list' hx-swap='innerHTML' style='display:flex;gap:0.4rem;margin-top:0.5rem;'><input type='hidden' name='room_id' value='{UI.escape(room_id)}' /><input name='username' placeholder='username to invite' class='module-select' style='flex:1;background:var(--bg);font-size:0.8rem;' /><button class='ui-btn'>Add</button></form></div>")
    return HTMLResponse(_ui_modal(inner, title=UI.escape(room_label(room))))

@router.post("/room/update", response_class=HTMLResponse)
async def room_update(request: Request, room_id: str = Form(...), title: str = Form(""), db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    room = db.get(Room, room_id)
    if not room or (room.owner != username and role not in ("admin","superadmin")): return HTMLResponse("Forbidden.", status_code=403)
    room.title = title.strip()[:120]; db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"rpRoomsUpdate": True, "rpRoomLabelUpdate": True})})

@router.post("/room/update_info", response_class=HTMLResponse)
async def room_update_info(request: Request, room_id: str = Form(...), mode: str = Form("general"), world: str = Form(""), world_detail: str = Form(""), db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    room = db.get(Room, room_id)
    if not room or (room.owner != username and role not in ("admin","superadmin")): return HTMLResponse("Forbidden.", status_code=403)
    save_info(room, {"mode": mode, "world": world.strip(), "world_detail": world_detail.strip()}, db)
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"rpRoomsUpdate": True})})

@router.post("/room/upload_bg", response_class=HTMLResponse)
async def upload_bg(request: Request, room_id: str = Form(...), bgfile: UploadFile = File(...), db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    room = db.get(Room, room_id)
    if not room or (room.owner != username and role not in ("admin","superadmin")): return HTMLResponse("Forbidden.", status_code=403)
    ext = pathlib.Path(bgfile.filename).suffix.lower()
    if ext not in (".png",".jpg",".jpeg",".webp",".gif"): return HTMLResponse("Invalid file type.", status_code=400)
    dest_dir = RP_ASSET_DIR / username; dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    with open(dest_dir / fname, "wb") as f: f.write(await bgfile.read())
    room.background = f"{RP_ASSET_URL}/{username}/{fname}"; db.commit()
    db.add(Asset(owner=username, filename=f"{username}/{fname}", asset_type="background")); db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "rpStageUpdate"})

@router.post("/room/member/add", response_class=HTMLResponse)
async def member_add(request: Request, room_id: str = Form(...), username: str = Form(...), db: Session = Depends(get_db)):
    me = _user(request); room = db.get(Room, room_id)
    if not room or room.owner != (me.username if me else ""): return HTMLResponse("Forbidden.", status_code=403)
    if not db.query(RoomMembership).filter(RoomMembership.room_id == room_id, RoomMembership.username == username).first():
        db.add(RoomMembership(room_id=room_id, username=username.strip())); db.commit()
    return await _member_list_html(room_id, db)

@router.post("/room/member/remove", response_class=HTMLResponse)
async def member_remove(request: Request, room_id: str = Form(...), username: str = Form(...), db: Session = Depends(get_db)):
    me = _user(request); room = db.get(Room, room_id)
    if not room or room.owner != (me.username if me else ""): return HTMLResponse("Forbidden.", status_code=403)
    db.query(RoomMembership).filter(RoomMembership.room_id == room_id, RoomMembership.username == username).delete(); db.commit()
    return await _member_list_html(room_id, db)

async def _member_list_html(room_id: str, db: Session) -> HTMLResponse:
    members = db.query(RoomMembership).filter(RoomMembership.room_id == room_id).all()
    if not members: return HTMLResponse('<div style="opacity:0.5;font-size:0.8rem;">No members yet.</div>')
    return HTMLResponse("".join(f'<div style="display:flex;justify-content:space-between;padding:0.2rem 0;"><span>{UI.escape(m.username)}</span><button class="ui-btn" style="padding:0.1rem 0.4rem;font-size:0.75rem;" hx-post="/module/rp_server/room/member/remove" hx-vals=\'{{"room_id":"{UI.escape(room_id)}","username":"{UI.escape(m.username)}"}}\' hx-target="#rp-member-list" hx-swap="innerHTML">&#x2715;</button></div>' for m in members))

# --- Options ---

@router.get("/options", response_class=HTMLResponse)
async def options_panel(request: Request, db: Session = Depends(get_db)):
    username = _username(request); role = _role(request)
    sess     = get_user_session(db, username)
    info     = sess.info or {}
    watches  = {w.room_id: w.notify for w in db.query(RoomWatch).filter(RoomWatch.username == username).all()}
    rooms    = [r for r in db.query(Room).all() if can_access_room(db, r, username, role)]
    watch_rows = "".join(f'<div style="display:flex;align-items:center;justify-content:space-between;padding:0.2rem 0;"><span style="font-size:0.85rem;">{UI.escape(room_label(r))}</span><label style="display:flex;align-items:center;gap:0.3rem;cursor:pointer;font-size:0.8rem;"><input type="checkbox" name="watch_{UI.escape(r.id)}" value="1" {"checked" if watches.get(r.id, False) else ""} /> notify</label></div>' for r in rooms)
    auto_scroll = "checked" if info.get("auto_scroll", False) else ""
    me_color    = info.get("bubble_me_color", "#00a0dc")
    other_color = info.get("bubble_other_color", "#00b464")
    notif_section = _ui_collapsible("Notifications", f"<p style='font-size:0.75rem;opacity:0.6;margin:0 0 0.5rem;'>Push notifications when someone posts while you're away. Requires PWA + notification permission.</p>{watch_rows}", open_=True)
    appear_section = _ui_collapsible("Appearance", _ui_field("My bubble color", f"<input type='color' name='bubble_me_color' value='{me_color}' style='width:3rem;height:2rem;border:none;background:none;cursor:pointer;' >") + _ui_field("Others bubble color", f"<input type='color' name='bubble_other_color' value='{other_color}' style='width:3rem;height:2rem;border:none;background:none;cursor:pointer;' >") + f"<label style='display:flex;align-items:center;gap:0.4rem;font-size:0.85rem;cursor:pointer;'><input type='checkbox' name='auto_scroll' value='1' {auto_scroll} > Auto-scroll on new message</label>")
    inner = f"<form hx-post='/module/rp_server/options/save' hx-swap='none' style='display:flex;flex-direction:column;gap:0.8rem;'>{notif_section}{appear_section}<div style='display:flex;justify-content:flex-end;'><button class='ui-btn' style='color:var(--accent);'>Save Settings</button></div></form>"
    return HTMLResponse(_ui_modal(inner, title="Options & Settings"))

@router.post("/options/save", response_class=HTMLResponse)
async def options_save(request: Request, db: Session = Depends(get_db)):
    username = _username(request)
    sess     = get_user_session(db, username)
    form     = await request.form()
    updates  = {k: str(form[k]) for k in ("bubble_me_color", "bubble_other_color") if form.get(k)}
    updates["auto_scroll"] = bool(form.get("auto_scroll"))
    save_info(sess, updates, db)
    for r in db.query(Room).all():
        wants    = bool(form.get(f"watch_{r.id}"))
        existing = db.query(RoomWatch).filter(RoomWatch.room_id == r.id, RoomWatch.username == username).first()
        if wants and not existing: db.add(RoomWatch(room_id=r.id, username=username, notify=True))
        elif not wants and existing: db.delete(existing)
        elif existing and existing.notify != wants: existing.notify = wants
    db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"closeModal": True})})

# --- Misc fragments ---

@router.get("/room/bridge", response_class=HTMLResponse)
async def room_bridge(request: Request, db: Session = Depends(get_db)):
    username = _username(request)
    sess     = get_user_session(db, username)
    return HTMLResponse(f'<input type="hidden" name="room_id" id="rp-room-bridge" value="{UI.escape(sess.current_room or "Public")}" />')

@router.get("/persona/label", response_class=HTMLResponse)
async def persona_label_fragment(request: Request, db: Session = Depends(get_db)):
    username = _username(request)
    sess     = get_user_session(db, username)
    return HTMLResponse(UI.escape(sess.current_persona or username))

@router.get("/diagnostics")
async def diagnostics(request: Request, db: Session = Depends(get_db)):
    ws = ENV.get("tools", {}).get("ws")
    return JSONResponse({"users": db.query(UserSession).count(), "rooms": db.query(Room).count(), "personas": db.query(Persona).count(), "messages": db.query(Message).count(), "ws": ws.get_stats() if ws else {"note": "ws not injected"}})