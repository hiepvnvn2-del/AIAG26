"""Live dashboard — React + Tailwind served as a single HTML file."""
from __future__ import annotations
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

DASHBOARD_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>RPA 2.0 — Live Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>
    *{font-family: ui-sans-system,-apple-system,Segoe UI,Roboto}
    .mono{font-family: ui-monospace,Menlo,Consolas,monospace}
    ::-webkit-scrollbar{width:6px;height:6px}
    ::-webkit-scrollbar-thumb{background:rgba(255,255,255,.18);border-radius:6px}
  </style>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen">
<div id="root"></div>
<script type="text/babel">
const { useEffect, useRef, useState } = React;

function Badge({ children, tone }){
  const map = {
    green: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
    amber: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    red: "bg-red-500/15 text-red-300 ring-red-500/30",
    zinc: "bg-zinc-700/60 text-zinc-300 ring-zinc-600",
  };
  return <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold tracking-wide ring-1 ${map[tone]||map.zinc}`}>{children}</span>;
}

function App(){
  const [events, setEvents] = useState([]);
  const [last, setLast] = useState({});
  const [hitl, setHitl] = useState(null);
  const [workflowId, setWorkflowId] = useState("");
  const [screenshots, setScreenshots] = useState({});
  const bottomRef = useRef(null);

  useEffect(()=>{
    const proto = location.protocol==="https:"?"wss://":"ws://";
    const ws = new WebSocket(proto + location.host + "/ws");
    ws.onmessage = (msg)=>{
      try{
        const evt = JSON.parse(msg.data);
        setEvents(prev=>[...prev.slice(-400), evt]);
        if(evt.kind==="hitl_request") setHitl(evt.payload);
        if(evt.kind==="hitl_resolved") setHitl(h=> h && h.workflow_id===evt.payload.workflow_id ? null : h);
        if(evt.workflow_id) setWorkflowId(evt.workflow_id);
        if(evt.kind==="thought"){
          const b64 = evt.payload?.image_b64 || evt.payload?.screenshot_b64;
          if(b64) setScreenshots(s=>({ ...s, [evt.payload.worker_id||"main"]: b64 }));
        }
        setLast(evt);
      }catch(e){}
    };
    return ()=>ws.close();
  },[]);

  useEffect(()=>{ bottomRef.current?.scrollIntoView({ behavior:"smooth" }); }, [events]);

  const sendDecision = async (decision)=>{
    if(!hitl) return;
    await fetch("/api/hitl/callback",{
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ workflow_id: hitl.workflow_id, decision, by: "dashboard" })
    });
  };

  const dispatchDemo = async ()=>{
    const txt = prompt("Nhap lenh tieng Viet hoac English (vi du: so sanh gia Hosting WP va cap nhat neu doi thu re hon 10%)","");
    if(!txt) return;
    await fetch("/api/run",{ method:"POST", headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ instruction: txt })});
  };

  return (
    <div className="max-w-[1400px] mx-auto p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 mb-4">
        <div>
          <div className="text-xl font-bold tracking-tight">Cross-Platform Web Action Agent</div>
          <div className="text-zinc-500 text-xs">RPA 2.0 — Live Worker Feed</div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-500 mono">{workflowId || "no workflow yet"}</span>
          <button onClick={dispatchDemo}
            className="rounded-xl bg-white text-zinc-900 px-4 py-2 text-sm font-semibold hover:bg-zinc-100">▶ Run demo workflow</button>
        </div>
      </div>

      {/* HITL gate */}
      {hitl && (
        <div className="rounded-2xl bg-amber-500/10 ring-1 ring-amber-400/30 p-4 mb-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-amber-300 font-semibold">⚠️ Yeu cau phe duyet</div>
              <div className="text-sm text-zinc-200 mt-1 mono break-words">{hitl.staged_action || "Staged action awaiting approval"}</div>
              <div className="text-xs text-zinc-500 mt-1">{hitl.impact_vn || ""}</div>
            </div>
            <div className="flex gap-2 shrink-0">
              <button onClick={()=>sendDecision("APPROVE")}
                className="rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-4 py-2 text-sm">✅ Duyet</button>
              <button onClick={()=>sendDecision("REJECT")}
                className="rounded-xl bg-zinc-700 hover:bg-zinc-600 text-white font-semibold px-4 py-2 text-sm">❌ Huy</button>
            </div>
          </div>
          {hitl.before && hitl.after && (
            <div className="mt-3 grid grid-cols-2 gap-3 text-xs mono">
              <div className="rounded-lg bg-zinc-900 p-2">before: {JSON.stringify(hitl.before)}</div>
              <div className="rounded-lg bg-zinc-900 p-2">after: {JSON.stringify(hitl.after)}</div>
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-12 gap-4">
        {/* Left: event stream */}
        <div className="col-span-12 lg:col-span-7 flex flex-col gap-4">
          <div className="rounded-2xl bg-zinc-900 ring-1 ring-zinc-800 p-4">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-semibold text-zinc-200">Worker event stream</div>
              <Badge tone="zinc">{events.length} events</Badge>
            </div>
            <div className="h-[520px] overflow-auto pr-1 space-y-1.5">
              {events.length===0 && <div className="text-zinc-500 text-sm">Chua co su kien — bam Run demo de bat dau.</div>}
              {events.map(e=>(
                <div key={e.id} className="rounded-xl bg-zinc-950 ring-1 ring-zinc-800/60 p-2">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge tone={
                      e.kind==="thought" ? "green" :
                      e.kind==="hitl_request" ? "amber" :
                      e.kind==="worker_error" ? "red" : "zinc"
                    }>{e.kind}</Badge>
                    <span className="text-[11px] text-zinc-500 mono truncate">{e.payload?.worker_id || ""}</span>
                    <span className="ml-auto text-[11px] text-zinc-600 mono">{new Date(e.ts*1000).toLocaleTimeString()}</span>
                  </div>
                  <div className="text-xs text-zinc-300 whitespace-pre-wrap break-words mono leading-relaxed">
                    {e.kind==="thought" ? (e.payload?.thought||"") : JSON.stringify(e.payload, null, 0).slice(0,700)}
                  </div>
                </div>
              ))}
              <div ref={bottomRef}></div>
            </div>
          </div>

          <div className="rounded-2xl bg-zinc-900 ring-1 ring-zinc-800 p-4">
            <div className="text-sm font-semibold text-zinc-200 mb-2">Latest payload</div>
            <pre className="mono text-xs text-zinc-300 whitespace-pre-wrap break-words max-h-[320px] overflow-auto bg-zinc-950 rounded-xl p-3">
{JSON.stringify(last?.payload || {}, null, 2)}
            </pre>
          </div>
        </div>

        {/* Right: screenshots + tips */}
        <div className="col-span-12 lg:col-span-5 flex flex-col gap-4">
          <div className="rounded-2xl bg-zinc-900 ring-1 ring-zinc-800 p-4">
            <div className="text-sm font-semibold text-zinc-200 mb-2">Screenshots (annotated)</div>
            {Object.keys(screenshots).length===0 ? (
              <div className="text-zinc-500 text-sm">Se hien thi khi worker chay.</div>
            ) : Object.entries(screenshots).map(([wid, b64])=>(
              <div key={wid} className="mb-3">
                <div className="text-xs text-zinc-400 mono mb-1">{wid}</div>
                <img src={`data:image/jpeg;base64,${b64}`} alt={`screenshot ${wid}`}
                  className="w-full rounded-xl ring-1 ring-zinc-800 object-contain max-h-[360px] bg-zinc-950"/>
              </div>
            ))}
          </div>

          <div className="rounded-2xl bg-zinc-900 ring-1 ring-zinc-800 p-4 text-xs leading-relaxed text-zinc-400">
            <div className="font-semibold text-zinc-200 mb-1">Stage-demo tips</div>
            <ul className="list-disc ml-4 space-y-1">
              <li>Competitor A has a blocking modal — the worker auto-dismisses it.</li>
              <li>Competitor B has infinite scroll/pagination — worker uses SCROLL_DOWN.</li>
              <li>Price updates pause at the Save button and wait for HITL approval.</li>
              <li>Default threshold 10%; proposed price = <span className="mono">min(competitor) × 0.98</span>.</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML
