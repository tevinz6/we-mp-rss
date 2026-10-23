<script setup lang="ts">
import {computed, onMounted, onUnmounted, ref} from 'vue'
type Row = Record<string, any>
const base='/api/v1/collection'
const ready=ref(false), setup=ref(false), password=ref(''), error=ref(''), notice=ref(''), busy=ref(false), mock=ref(false)
const tab=ref('articles'), overview=ref<Row>({}), feeds=ref<Row[]>([]), jobs=ref<Row[]>([]), events=ref<Row[]>([]), sources=ref<Row[]>([])
const articles=ref<Row[]>([]), total=ref(0), offset=ref(0), keyword=ref(''), filter=ref(''), selected=ref<Row|null>(null)
const searchName=ref(''), matches=ref<Row[]>([]), qr=ref(''), qrMessage=ref(''), importUrl=ref(''), importFeed=ref('')
const since=ref(''), until=ref(''), searchSource=ref('primary'), selectedSource=ref('primary'), qrSource=ref(''), manualBook=ref(''), manualName=ref('')
const sourceName=(id:string)=>id==='weread'?'微信读书':'微信公众平台'
let qrGeneration=0
let timer:ReturnType<typeof setInterval>|undefined, qrTimer:ReturnType<typeof setInterval>|undefined
const stateNames:Record<string,string>={active:'可采集',unconfigured:'待授权',queued:'等待执行',running:'正在执行',succeeded:'已完成',partial:'部分完成',failed:'失败',cancelled:'已取消',retry_wait:'等待重试',blocked_auth:'等待授权',blocked_rate:'频率受限',blocked_challenge:'需要验证',blocked_source:'已暂停',saved:'已保存',pending:'待补采',deleted:'原文已删除',unavailable:'原文不可用',remote:'远程图片',unknown:'纯文本归档 · 图片状态未知',none:'无图片',paged_list:'分页列表',unverified:'待验证',weread_list:'微信读书列表',latest_only:'仅最新一篇 · 无历史覆盖'}
const kinds:Record<string,string>={discover:'检查文章',fetch:'保存正文',import:'导入文章',archive_image:'归档图片'}
const source=computed(()=>sources.value.find(s=>s.id===selectedSource.value)||{})
const date=(v:number|null)=>v?new Date(v*1000).toLocaleString('zh-CN',{hour12:false}):'尚无记录'
const feedName=(id:string)=>{const feed=feeds.value.find(x=>x.id===id);return feed?`${feed.name} · ${sourceName(feed.source_id)}`:'—'}
async function api(path:string,method='GET',body?:unknown){
 const response=await fetch(path.startsWith('/api/')?path:base+path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined})
 if(response.status===401)ready.value=false
 const data=await response.json()
 if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:data.detail?.message||'操作未完成，请检查输入或稍后重试')
 return data
}
async function action(fn:()=>Promise<void>){busy.value=true;error.value='';notice.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function loadArticles(){
 const params=new URLSearchParams({q:keyword.value,offset:String(offset.value),limit:'20'})
 if(filter.value)params.set('feed_id',filter.value)
 if(since.value)params.set('since',String(new Date(since.value+'T00:00:00').getTime()/1000))
 if(until.value)params.set('until',String(new Date(until.value+'T23:59:59').getTime()/1000))
 const data=await api('/articles?'+params);articles.value=data.items;total.value=data.total
}
async function refresh(){
 const result=await Promise.all([api('/overview'),api('/feeds'),api('/jobs'),api('/events'),api('/sources')])
 ;[overview.value,feeds.value,jobs.value,events.value,sources.value]=result
 mock.value=overview.value.mock
 await loadArticles()
}
async function login(){await action(async()=>{await api(setup.value?'/api/setup':'/api/login','POST',{password:password.value});password.value='';ready.value=true;await refresh()})}
async function search(){await action(async()=>{matches.value=(await api('/search','POST',{keyword:searchName.value,source_id:searchSource.value})).accounts})}
async function add(match:Row){await action(async()=>{await api('/feeds','POST',{external_id:match.external_id,name:match.name,source_id:match.source_id||searchSource.value});matches.value=[];notice.value='公众号已添加，可开始检查文章。';await refresh()})}
async function sync(feed:Row,history=false){await action(async()=>{await api(`/feeds/${feed.id}/sync`,'POST',{mode:history?'history':'incremental',pages:2,days:30});notice.value='任务已加入队列。';await refresh()})}
async function settings(feed:Row){await action(async()=>{await api(`/feeds/${feed.id}/settings`,'PUT',{enabled:feed.enabled,interval_minutes:Number(feed.interval_minutes),archive_images:feed.archive_images,collection_mode:feed.collection_mode});notice.value='配置已保存。若来源已暂停，请到来源与授权中手动恢复。';await refresh()})}
async function openArticle(id:string){await action(async()=>{selected.value=await api('/articles/'+id)})}
async function refreshArticle(){await action(async()=>{await api(`/articles/${selected.value!.id}/refresh`,'POST');notice.value='正文补采任务已加入队列。';await refresh()})}
async function jobAction(job:Row,which:string){await action(async()=>{await api(`/jobs/${job.id}/${which}`,'POST');await refresh()})}
async function sourceAction(which:string){await action(async()=>{await api('/sources/'+selectedSource.value+'/'+which,'POST');await refresh()})}
async function startQr(){await action(async()=>{
 if(qrTimer)clearTimeout(qrTimer)
 const generation=++qrGeneration
 qrSource.value=selectedSource.value
 const path=qrSource.value==='weread'?'/sources/weread/auth/qr':'/auth/qr'
 const result=await api(path,'POST');qr.value=result.image
 qrMessage.value=qrSource.value==='weread'?'请使用微信扫码，确认登录微信读书。':'请使用有公众号管理权限的微信扫码并确认。'
 const expires=Date.now()+result.expires_in*1000
 async function poll(){
  if(generation!==qrGeneration)return
  if(Date.now()>expires){qr.value='';qrMessage.value='二维码已过期，请重新生成。';return}
  try{
   const data=await api(path+'/check','POST')
   if(generation!==qrGeneration)return
   if(data.status==='success'){qr.value='';qrMessage.value='授权成功。可到公众号页面搜索微信读书书架。';await refresh();return}
   if(data.status==='expired'){qr.value='';qrMessage.value='二维码已过期';return}
   if(data.status==='scanned')qrMessage.value='已扫码，请在微信中确认。'
   qrTimer=setTimeout(poll,5000)
  }catch(e){if(generation!==qrGeneration)return;qr.value='';error.value=(e as Error).message;await refresh()}
 }
 qrTimer=setTimeout(poll,5000)
 await refresh()
})}
function changeSource(){qrGeneration++;if(qrTimer)clearTimeout(qrTimer);qr.value='';qrMessage.value=''}
async function addManual(){await add({external_id:manualBook.value.trim(),name:manualName.value.trim(),source_id:'weread'})}
async function importArticle(){await action(async()=>{await api('/imports','POST',{feed_id:importFeed.value,url:importUrl.value});importUrl.value='';notice.value='文章导入任务已加入队列。';await refresh()})}
async function rssLink(feed:Row){await action(async()=>{const data=await api(`/feeds/${feed.id}/rss-link`,'POST');await navigator.clipboard.writeText(data.url);notice.value='只读 RSS 订阅地址已复制。此地址可读取该公众号归档，请仅提供给自己的阅读器。'})}
async function download(feed:Row,format:string){await action(async()=>{const response=await fetch(`${base}/exports/${feed.id}?format=${format}`);if(!response.ok)throw Error('导出失败，请重新登录后重试');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=`${feed.name}.${format==='markdown'?'md':format==='rss'?'xml':'json'}`;a.click();URL.revokeObjectURL(url)})}
onMounted(async()=>{await action(async()=>{const info=await api('/api/setup');setup.value=info.required;mock.value=info.mock;if(!info.required){try{await refresh();ready.value=true}catch{ready.value=false}}});timer=setInterval(()=>{if(ready.value&&!busy.value)refresh().catch(()=>{})},10000)})
onUnmounted(()=>{qrGeneration++;clearInterval(timer);clearTimeout(qrTimer)})
</script>

<template>
 <div v-if="!ready" class="login-shell"><div class="login-card"><span class="eyebrow">READ · COLLECT · KEEP</span><h1>公众号采集工作台</h1><p>{{setup?'先设置管理员密码，开启你的文章资料库。':'登录后管理订阅、采集任务与文章归档。'}}</p><div v-if="mock" class="banner">本地演示 · 不会请求微信</div><form @submit.prevent="login"><label>管理员密码<input v-model="password" type="password" minlength="12" maxlength="128" autocomplete="current-password" placeholder="至少 12 位" required></label><button class="primary" :disabled="busy">{{setup?'创建工作台':'登录'}}</button></form><p v-if="error" class="error" role="alert">{{error}}</p></div></div>
 <div v-else class="shell">
  <aside><div class="brand"><span class="brand-mark">阅</span><div><b>文章资料库</b><small>公众号采集工作台</small></div></div><nav><button v-for="item in [['articles','文章归档','01'],['feeds','公众号','02'],['jobs','采集任务','03'],['sources','来源与授权','04'],['events','异常记录','05']]" :key="item[0]" :class="{active:tab===item[0]}" @click="tab=item[0];selected=null"><span>{{item[2]}}</span>{{item[1]}}</button></nav><div class="aside-foot"><span class="dot" :class="{warn:source.state!=='active'}"></span>{{mock?'模拟来源':stateNames[source.state]||'待授权'}}<p>内容归档于本机<br>最后更新 {{new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}}</p><button class="text-button" @click="action(async()=>{await api('/api/logout','POST');ready=false})">退出登录</button></div></aside>
  <main><header><div><span class="eyebrow">YOUR READING ARCHIVE</span><h1>{{({articles:'值得留存的内容',feeds:'关注你关心的公众号',jobs:'每一次采集都有记录',sources:'管理采集来源',events:'需要关注的变化'} as Record<string,string>)[tab]}}</h1><p>从订阅到归档，让文章有序积累。</p></div><button class="secondary" :disabled="busy" @click="action(refresh)">刷新状态 ↻</button></header>
   <div v-if="mock" class="banner">演示模式：当前文章来自本地模拟数据，没有访问微信。真实采集需在独立数据目录中启动并扫码授权。</div>
   <div v-if="error" role="alert" class="alert error">{{error}}<button @click="error=''">×</button></div><div v-if="notice" role="status" class="alert success">{{notice}}</div>
   <div class="stats"><div><span>订阅公众号</span><b>{{overview.feeds||0}}<small> 个</small></b></div><div><span>已发现文章</span><b>{{overview.articles||0}}<small> 篇</small></b></div><div><span>正文已保存</span><b>{{overview.saved||0}}<small> 篇</small></b></div><div><span>待处理任务</span><b>{{overview.pending||0}}<small> 项</small></b></div></div>

   <section v-if="tab==='articles'&&!selected" class="panel"><div class="section-heading"><h2>文章归档 <span>{{total}}</span></h2><span class="muted">按发布时间排列</span></div><form class="filters" @submit.prevent="offset=0;action(loadArticles)"><input v-model="keyword" placeholder="搜索标题或正文…" aria-label="搜索文章"><select v-model="filter" aria-label="公众号筛选"><option value="">全部公众号</option><option v-for="feed in feeds" :value="feed.id">{{feed.name}} · {{sourceName(feed.source_id)}}</option></select><input type="date" v-model="since" aria-label="起始日期"><input type="date" v-model="until" aria-label="截止日期"><button class="secondary">查询</button></form>
    <div v-if="!articles.length" class="empty"><span>文</span><h3>文章会在这里慢慢积累</h3><p>先添加公众号，再启动一次采集。若已有任务，请检查采集服务是否运行。</p><button class="primary" @click="tab='feeds'">管理公众号</button></div>
    <button v-for="article in articles" :key="article.id" class="article-row" @click="openArticle(article.id)"><div class="article-icon">文</div><div class="article-info"><div class="article-meta">{{feedName(article.feed_id)}} · {{article.published_at?date(article.published_at):'发布时间未提供'}}</div><h3>{{article.title}}</h3><div class="article-meta">{{article.author||'作者未提供'}} · {{stateNames[article.media_status]||article.media_status}}</div></div><span class="badge" :class="article.content_status">{{stateNames[article.content_status]||article.content_status}}</span><span class="arrow">↗</span></button>
    <div class="pagination"><button :disabled="offset===0" @click="offset-=20;action(loadArticles)">上一页</button><span>{{Math.floor(offset/20)+1}} / {{Math.max(1,Math.ceil(total/20))}}</span><button :disabled="offset+20>=total" @click="offset+=20;action(loadArticles)">下一页</button></div>
   </section>
   <section v-if="tab==='articles'&&selected" class="panel reader"><button class="text-button" @click="selected=null">← 返回文章列表</button><h2>{{selected.title}}</h2><p class="muted">{{feedName(selected.feed_id)}} · {{selected.published_at?date(selected.published_at):'发布时间未提供'}}</p><div class="actions"><a :href="selected.url" target="_blank" rel="noopener noreferrer">查看原文 ↗</a><button @click="refreshArticle" :disabled="busy">重新获取正文</button><span class="badge">{{selected.versions.length}} 个归档版本</span></div><p v-if="selected.last_error" class="error">{{selected.last_error}}</p><div v-if="selected.content_html" class="article-body" v-html="selected.content_html"></div><div v-else class="empty">正文尚未保存，可以发起补采。</div></section>

   <section v-if="tab==='feeds'" class="panel"><div class="section-heading"><h2>添加公众号</h2><span class="muted">搜索后确认，避免重名</span></div><form class="filters" @submit.prevent="search"><select v-model="searchSource" aria-label="搜索来源" @change="matches=[]"><option value="primary">微信公众平台</option><option value="weread">微信读书书架</option></select><input v-model="searchName" placeholder="输入公众号名称" required><button class="primary" :disabled="busy">搜索公众号</button></form><p v-if="searchSource==='weread'" class="muted">在已授权微信读书账号的书架中查找公众号，不是全网搜索。没有结果时，请先在微信读书中关注目标公众号，再搜索；是否可采集以实际列表返回为准。</p><div v-for="match in matches" class="match"><b>{{match.name}}</b><span>{{match.alias}}</span><button @click="add(match)" :disabled="busy">添加</button></div>
    <details v-if="searchSource==='weread'" class="note"><summary>已有微信读书公众号标识？手动添加</summary><form class="filters" @submit.prevent="addManual"><input v-model="manualBook" placeholder="MP_WXS_数字" pattern="MP_WXS_[0-9]+" required><input v-model="manualName" placeholder="公众号名称" required><button :disabled="busy">添加微信读书订阅</button></form><p>标识来自微信读书公众号页面的 bookId，不是公众平台的 fakeid。</p></details>
    <div v-for="feed in feeds" class="feed-card"><div class="section-heading"><div><h3>{{feed.name}}</h3><p class="muted">{{sourceName(feed.source_id)}}<br>最近检查：{{date(feed.last_check_at)}}<br>最近发现新文章：{{date(feed.last_new_at)}} · {{stateNames[feed.coverage]||feed.coverage}}</p></div><span class="badge">{{feed.enabled?'已启用':'已暂停'}}</span></div><div class="feed-settings"><label v-if="feed.source_id==='weread'">采集范围<select v-model="feed.collection_mode" :aria-label="feed.name+'采集范围'"><option value="list">文章列表</option><option value="latest">仅最新一篇</option></select></label><label>检查间隔（分钟）<input type="number" min="60" max="10080" v-model="feed.interval_minutes"></label><label class="check"><input type="checkbox" v-model="feed.enabled">启用采集</label><label class="check"><input type="checkbox" v-model="feed.archive_images">归档图片</label><button @click="settings(feed)" :disabled="busy">保存设置</button></div><div class="actions"><button class="primary" @click="sync(feed)" :disabled="busy||!feed.enabled">检查新文章</button><button @click="sync(feed,true)" :disabled="busy||!feed.enabled||feed.collection_mode==='latest'">补采近 30 天 · 每次 2 页</button><button @click="download(feed,'json')">JSON</button><button @click="download(feed,'markdown')">Markdown</button><button @click="download(feed,'rss')">RSS 文件</button><button @click="rssLink(feed)">复制 RSS 地址</button></div><p v-if="feed.collection_mode==='latest'" class="muted">仅保存检查时最新的一篇，无法补采历史；两次检查之间可能遗漏文章。来源未提供发布时间时，不显示推测日期。</p></div><div v-if="!feeds.length" class="empty">还没有订阅，搜索并添加第一个公众号。</div>
    <div class="import"><h3>导入单篇文章</h3><p class="muted">已知链接可单独归档；不会自动订阅对应公众号。</p><form class="filters" @submit.prevent="importArticle"><select v-model="importFeed" required><option value="">选择归属公众号</option><option v-for="feed in feeds" :value="feed.id">{{feed.name}} · {{sourceName(feed.source_id)}}</option></select><input v-model="importUrl" type="url" placeholder="https://mp.weixin.qq.com/s/…" required><button :disabled="busy">导入</button></form></div>
   </section>

   <section v-if="tab==='jobs'" class="panel"><div class="section-heading"><h2>最近任务</h2><span class="muted">显示最近 50 项 · 每 10 秒更新</span></div><div class="table-wrap"><table><thead><tr><th>任务 / 公众号</th><th>状态</th><th>开始时间</th><th>结果</th><th>操作</th></tr></thead><tbody><tr v-for="job in jobs"><td><b>{{kinds[job.kind]||job.kind}}</b><small>{{feedName(job.feed_id)}}</small></td><td><span class="badge" :class="job.status">{{stateNames[job.status]||job.status}}</span></td><td>{{date(job.started_at)}}</td><td>{{job.error_message||(job.result.inserted!==undefined?`新增 ${job.result.inserted} 篇`:job.result.continuation_required?'需继续补采':'—')}}<small v-if="job.result.continuation_required">已达本轮页数上限，可继续补采</small></td><td><button v-if="['failed','partial','cancelled'].includes(job.status)" @click="jobAction(job,'retry')" :disabled="busy">重试</button><button v-else-if="job.status!=='succeeded'" @click="jobAction(job,'cancel')" :disabled="busy">取消</button></td></tr></tbody></table></div><div v-if="!jobs.length" class="empty">还没有采集任务。</div></section>
   <section v-if="tab==='sources'" class="panel"><div class="section-heading"><h2>{{mock?'本地模拟来源':sourceName(selectedSource)}}</h2><select v-model="selectedSource" aria-label="授权来源" @change="changeSource"><option value="primary">微信公众平台</option><option value="weread">微信读书</option></select><span class="badge" :class="source.state">{{stateNames[source.state]||source.state}}</span></div><p class="muted">{{source.reason||'仅在计划执行或手动操作时请求来源。'}}</p><p v-if="source.paused_until">最早可恢复：{{date(source.paused_until)}}</p><div class="actions"><button v-if="!mock" class="primary" @click="startQr" :disabled="busy">扫码授权</button><button @click="sourceAction('pause')" :disabled="busy||source.state!=='active'">暂停采集</button><button @click="sourceAction('resume')" :disabled="busy||source.state==='active'">恢复采集</button></div><div v-if="qr" class="qr"><img :src="qr" :alt="sourceName(qrSource)+'登录二维码'" style="background:white"></div><p>{{qrMessage}}</p><div class="note"><h3>登录状态和采集状态分开管理</h3><p>登录过期后需重新扫码。出现频率限制或验证要求时，系统暂停相关任务；处理完成后再手动恢复。</p><p>两个渠道分别授权、暂停和恢复。微信读书从书架查找公众号，获取列表或最新一篇及正文；采集范围需在订阅设置中明确选择，不会在错误后自动切换渠道或模式。实际覆盖范围需要登录后验证。</p></div></section>
   <section v-if="tab==='events'" class="panel"><div class="section-heading"><h2>异常与提醒</h2><span class="muted">相同未解决问题合并记录</span></div><div v-for="event in events" class="event"><span class="event-dot" :class="{resolved:event.resolved_at}"></span><div><h3>{{event.message}}</h3><p class="muted">{{date(event.created_at)}} · {{event.resolved_at?'已恢复':'待处理'}} · {{event.code}}</p></div></div><div v-if="!events.length" class="empty">暂时没有异常记录。</div></section>
   <footer>内容覆盖范围取决于来源实际可访问数据。登录失效、验证要求和访问限制会暂停采集。</footer>
  </main>
 </div>
</template>
