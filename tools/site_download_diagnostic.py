#!/usr/bin/env python3
"""Inspect TopDJMixes' own login form and Download-button JavaScript without submitting forms."""
import json
import re
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

BASE = "https://www.topdjmixes.com"
EPISODE = BASE + "/the-midnight-project-228/"
LOGIN = BASE + "/login/?redirect_to=" + requests.utils.quote(EPISODE, safe="")
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})
def emit(event, **kw):
    print(json.dumps({"event": event, **kw}, ensure_ascii=False, sort_keys=True), flush=True)
def safe_url(u):
    p=urlparse(u)
    return {"host":p.netloc, "path":p.path, "query_present":bool(p.query)}
def get(url):
    try:
        r=S.get(url,timeout=25,allow_redirects=True)
        emit("http",requested=safe_url(url),status=r.status_code,final=safe_url(r.url),
             content_type=r.headers.get("content-type",""),bytes=len(r.content))
        return r
    except Exception as e:
        emit("http_error",url=safe_url(url),error=type(e).__name__,detail=str(e)[:120])
        return None

# Read the public episode template and identify its actual visible Download/login control.
r=get(EPISODE)
if r and r.status_code < 400:
    soup=BeautifulSoup(r.text,"html.parser")
    matches=[]
    for el in soup.select("a,button,input,[role=button]"):
        text=(el.get_text(" ",strip=True) or el.get("value") or "").strip()
        attrs=" ".join(str(el.get(k,"")) for k in ("id","class","href","onclick","data-id","data-post-id","data-action","name"))
        if re.search(r"download|please login|create a free account",text+" "+attrs,re.I):
            matches.append({
                "tag":el.name,"text":text[:100],"id":el.get("id",""),
                "class":" ".join(el.get("class",[]))[:150],
                "href":safe_url(urljoin(r.url,el.get("href",""))) if el.get("href") else None,
                "onclick":(el.get("onclick") or "")[:200],
                "data_attrs":{k:v for k,v in el.attrs.items() if k.startswith("data-")},
                "type":el.get("type",""),"name":el.get("name","")
            })
    emit("episode_download_markup",title=soup.title.get_text(" ",strip=True) if soup.title else "",
         controls=matches[:35],login_gate="please login to download or create a free account" in soup.get_text(" ",strip=True).lower())
    js_url=BASE+"/wp-content/themes/topdjmixes/download-post.js?ver=7.1.3"
    js=get(js_url)
    if js and js.status_code < 400:
        emit("download_post_js_source",url=safe_url(js.url),source=js.text[:16000])
    main=get(BASE+"/wp-content/themes/topdjmixes/js/main.js?ver=1.0.0")
    if main and main.status_code < 400:
        text=main.text
        hits=[]
        for marker in ("download-post","download_post","admin-ajax.php","wp_ajax","nonce","download"):
            for m in list(re.finditer(re.escape(marker),text,re.I))[:8]:
                hits.append({"marker":marker,"snippet":text[max(0,m.start()-180):min(len(text),m.end()+260)]})
        emit("main_js_download_markers",hits=hits[:35])

# Inspect initial login state and its real form structure; DO NOT submit it.
r=get(LOGIN)
if r and r.status_code < 400:
    soup=BeautifulSoup(r.text,"html.parser")
    forms=[]
    for form in soup.select("form"):
        fs=[]
        for el in form.select("input[name],button[name],select[name],textarea[name]"):
            fs.append({"tag":el.name,"name":el.get("name",""),"type":el.get("type",el.name),
                       "hidden":(el.get("type") or "").lower()=="hidden",
                       "value_present":bool(el.get("value")),
                       "value_length":len(el.get("value",""))})
        if any(("user_login" in x["name"] or "user_pass" in x["name"] or x["name"].startswith("arm_")) for x in fs):
            forms.append({"id":form.get("id",""),"class":" ".join(form.get("class",[]))[:120],
                          "action":safe_url(urljoin(r.url,form.get("action",""))),
                          "method":form.get("method","get"),"fields":fs[:50]})
    script_urls=[urljoin(r.url,x.get("src","")) for x in soup.select("script[src]")]
    relevant=[safe_url(u) for u in script_urls if any(s in u.lower() for s in ("arm","membership","login","jquery.form"))]
    inline=[]
    for script in soup.select("script:not([src])"):
        t=script.string or script.get_text()
        if any(m in t.lower() for m in ("arm_form_submission_key","arm_nonce_check","arm_action","spam detected")):
            inline.append(t[:1800])
    body=soup.get_text(" ",strip=True).lower()
    emit("login_initial_state",title=soup.title.get_text(" ",strip=True) if soup.title else "",
         spam_detected_before_submit="spam detected" in body,forms=forms[:10],
         relevant_scripts=relevant[:35],inline_form_script_excerpts=inline[:12],
         cookies_seen=sorted({c.name for c in S.cookies}))
    # Read public scripts linked from the login form page and report only relevant snippets.
    candidates=[u for u in script_urls if any(s in u.lower() for s in ("arm","membership","login","jquery.form"))]
    for u in candidates[:12]:
        js=get(u)
        if not js or js.status_code>=400 or len(js.content)>900000:
            continue
        t=js.text
        keys=("arm_form_submission_key","arm_nonce_check","arm_action","spam detected","honeypot","form_filter_st","j5khzvhz")
        snippets=[]
        for key in keys:
            for m in list(re.finditer(re.escape(key),t,re.I))[:3]:
                snippets.append({"marker":key,"snippet":t[max(0,m.start()-220):min(len(t),m.end()+260)]})
        if snippets:
            emit("login_js_markers",script=safe_url(js.url),hits=snippets[:24])
