"""Dependency-free HTML shell for the local Asset Library."""
from __future__ import annotations

from fastapi.responses import HTMLResponse

ASSET_LIBRARY_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Content OS · 素材库</title>
<style>
body{font:15px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222;background:#fafafa}
.toolbar{display:flex;gap:.5rem;flex-wrap:wrap}.toolbar input{flex:1;min-width:260px;padding:.6rem}.asset,.result{background:white;border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}.clip{border-top:1px solid #eee;padding:.7rem 0}.tags,.muted{color:#666}video{max-width:100%;max-height:420px;background:#111}button{padding:.55rem .8rem;cursor:pointer}.error{color:#a21}
</style></head>
<body><h1>Content OS 素材库</h1>
<div class="toolbar"><input id="query" aria-label="搜索素材" placeholder="例如：本人坐在电脑前操作软件"><button id="search">语义搜索</button><button id="reload">刷新素材</button></div>
<section id="results" aria-live="polite"></section><main id="assets">正在加载…</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>`${Math.floor(n/60000)}:${String(Math.floor(n/1000)%60).padStart(2,'0')}.${String(n%1000).padStart(3,'0')}`;
const tags=c=>[...(c.people||[]),...(c.objects||[]),c.location,c.action,c.shot_type,c.visual_description].filter(Boolean).join(' · ');
function bindPreview(root=document){root.querySelectorAll('button[data-start]').forEach(b=>b.onclick=()=>{const v=document.querySelector(`video[data-asset="${b.dataset.asset}"]`);if(!v)return;v.currentTime=Number(b.dataset.start)/1000;v.play();const end=Number(b.dataset.end)/1000;const stop=()=>{if(v.currentTime>=end){v.pause();v.removeEventListener('timeupdate',stop)}};v.addEventListener('timeupdate',stop)})}
async function load(){const root=document.querySelector('#assets');try{const response=await fetch('/assets');if(!response.ok)throw new Error('素材读取失败');const assets=await response.json();root.innerHTML=assets.map(a=>`<section class="asset"><h2>${esc(a.source_file)}</h2><p>${a.width}×${a.height} · ${fmt(a.duration_ms)}</p><video data-asset="${a.id}" controls preload="metadata" src="/assets/${a.id}/media"></video><div id="clips-${a.id}">正在加载片段…</div></section>`).join('')||'<p>尚无素材。</p>';for(const a of assets){const response=await fetch(`/assets/${a.id}/clips`);const clips=response.ok?await response.json():[];const target=document.querySelector('#clips-'+a.id);target.innerHTML=clips.map(c=>`<article class="clip"><button data-asset="${a.id}" data-start="${c.start_ms}" data-end="${c.end_ms}">${fmt(c.start_ms)} – ${fmt(c.end_ms)}</button> <strong>${esc(c.transcript||'')}</strong><p class="tags">${esc(tags(c))}</p></article>`).join('')||'<p class="muted">尚无片段。</p>';}bindPreview(root)}catch(error){root.innerHTML=`<p class="error">${esc(error.message)}</p>`}}
async function search(){const root=document.querySelector('#results');const query=document.querySelector('#query').value.trim();if(!query){root.innerHTML='<p class="error">请输入搜索内容。</p>';return}root.textContent='正在搜索…';try{const response=await fetch('/clips/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,top_k:10})});const body=await response.json();if(!response.ok)throw new Error(body.detail||'搜索失败');root.innerHTML=`<h2>搜索结果</h2>`+(body.map(h=>`<article class="result"><strong>${esc(h.clip.transcript||h.clip.visual_description||'未命名片段')}</strong><p>${fmt(h.clip.start_ms)} – ${fmt(h.clip.end_ms)} · 匹配度 ${Number(h.score).toFixed(3)}</p><p class="tags">${esc(tags(h.clip))}</p><a href="/clips/${h.clip.id}/media" target="_blank" rel="noopener">打开原视频</a></article>`).join('')||'<p>没有匹配片段。</p>')}catch(error){root.innerHTML=`<p class="error">${esc(error.message)}</p>`}}
document.querySelector('#reload').onclick=load;document.querySelector('#search').onclick=search;document.querySelector('#query').onkeydown=event=>{if(event.key==='Enter')search()};load();
</script></body></html>"""


def asset_library_page() -> HTMLResponse:
    return HTMLResponse(ASSET_LIBRARY_HTML)
