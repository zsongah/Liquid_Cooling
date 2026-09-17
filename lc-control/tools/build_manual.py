"""将本项目手册转换成可离线阅读的 HTML；仅支持本手册使用的 Markdown 子集。

这是文档构建工具，不是软件运行依赖，不读取设备也不执行控制动作。
支持标题、段落、列表、表格、代码块、链接和强调；所有普通文本 HTML 转义。
"""
import html
import re
from pathlib import Path
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'docs/边缘液冷智控产品审查与使用手册.md'
VERSION=re.search(r'^version\s*=\s*"([^"]+)"', (ROOT/'pyproject.toml').read_text(), re.M).group(1)


def inline(text):
    """先隔离代码/链接，再转义普通文本，避免文档内容成为 HTML。"""
    held=[]
    def save(value):
        held.append(value)
        return '\x00%s\x00' % (len(held)-1)
    def code(match):
        label=match.group(1)
        rendered='<code>'+html.escape(label)+'</code>'
        if (ROOT/label).is_file() and not Path(label).is_absolute():
            rendered='<a href="../'+quote(label,safe='/')+'">'+rendered+'</a>'
        return save(rendered)
    text=re.sub(r'`([^`]+)`',code,text)
    def link(match):
        label,url=match.groups()
        if not (url.startswith(('https://','http://','#','../')) or ':' not in url):
            return html.escape(label)
        return save('<a href="'+html.escape(url,quote=True)+'">'+html.escape(label)+'</a>')
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,text)
    text=html.escape(text)
    text=re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',text)
    return re.sub(r'\x00(\d+)\x00',lambda m:held[int(m.group(1))],text)


def render(text):
    """解析已知文档结构；返回正文 HTML 和二级章节目录。"""
    lines=text.splitlines();out=[];toc=[];i=0;counter=0
    while i<len(lines):
        line=lines[i]
        if not line.strip():i+=1;continue
        if line.startswith('```'):
            language=line[3:].strip();i+=1;block=[]
            while i<len(lines) and not lines[i].startswith('```'):
                block.append(lines[i]);i+=1
            out.append('<div class="code-label">'+html.escape(language or '示意')+'</div><pre><code>'+html.escape('\n'.join(block))+'</code></pre>');i+=1;continue
        heading=re.match(r'^(#{1,4}) (.+)',line)
        if heading:
            level=len(heading.group(1));label=heading.group(2);counter+=1
            # 二级章节编号用作稳定锚点，插入三级标题不会破坏门户链接。
            section=re.match(r'^(\d+)\.',label) if level==2 else None
            key='section-'+section.group(1) if section else 'chapter-'+str(counter)
            out.append('<h{0} id="{1}">{2}</h{0}>'.format(level,key,inline(label)))
            if level==2:toc.append((key,label))
            i+=1;continue
        if line.startswith('|'):
            table=[]
            while i<len(lines) and lines[i].startswith('|'):
                row=lines[i].strip().strip('|').split('|')
                if not all(re.fullmatch(r'\s*:?-+:?\s*',c) for c in row):table.append(row)
                i+=1
            head='<thead><tr>'+''.join('<th>'+inline(c.strip())+'</th>' for c in table[0])+'</tr></thead>'
            body='<tbody>'+''.join('<tr>'+''.join('<td>'+inline(c.strip())+'</td>' for c in row)+'</tr>' for row in table[1:])+'</tbody>'
            out.append('<div class="tablewrap"><table>'+head+body+'</table></div>');continue
        item=re.match(r'^(?:- |\d+\. )(.+)',line)
        if item:
            tag='ul' if line.startswith('- ') else 'ol';items=[]
            while i<len(lines):
                m=re.match(r'^- (.+)' if tag=='ul' else r'^\d+\. (.+)',lines[i])
                if not m:break
                items.append('<li>'+inline(m.group(1))+'</li>');i+=1
            out.append('<'+tag+'>'+''.join(items)+'</'+tag+'>');continue
        paragraph=[]
        while i<len(lines) and lines[i].strip() and not re.match(r'^(#|```|\||- |\d+\. )',lines[i]):
            paragraph.append(lines[i]);i+=1
        out.append('<p>'+inline(' '.join(paragraph))+'</p>')
    return '\n'.join(out),toc


def main():
    source=SOURCE.read_text()
    body,toc=render(source)
    reviewed=re.search(r'核对日期：(\d{4}-\d{2}-\d{2})',source).group(1)
    navigation=''.join('<a href="#'+key+'">'+html.escape(label)+'</a>' for key,label in toc)
    page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>边缘液冷智控软件｜产品审查与使用手册</title><style>
:root{--ink:#19353e;--muted:#657c84;--teal:#087b7e;--line:#dbe5e7}*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:25px}body{margin:0;background:#f4f6f7;color:var(--ink);font:15px/1.9 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}aside{position:fixed;inset:0 auto 0 0;width:262px;padding:30px 20px;background:#e8eff0;border-right:1px solid var(--line);overflow:auto}.brand{font-size:12px;color:var(--teal);letter-spacing:1.3px;font-weight:700}aside h2{font-size:20px;line-height:1.5;margin:15px 0}nav a{display:block;font-size:12px;line-height:1.6;color:#49656d;text-decoration:none;padding:7px 8px;border-radius:4px}nav a:hover{background:#d7e8e6}main{margin-left:262px;padding:38px 46px 70px;max-width:1490px}article{background:#fff;border:1px solid var(--line);border-radius:12px;padding:35px 40px}h1{font-size:34px;line-height:1.45;margin:0 0 20px;letter-spacing:-.6px}h2{font-size:24px;line-height:1.5;margin:42px 0 18px;border-top:1px solid var(--line);padding-top:26px}h3{font-size:18px;margin:28px 0 12px}p{margin:13px 0}a{color:var(--teal);text-underline-offset:3px}li{margin:9px 0}ul,ol{padding-left:23px}.tablewrap{overflow:auto;border:1px solid var(--line);border-radius:6px;margin:18px 0}table{border-collapse:collapse;width:100%;font-size:13px;line-height:1.8;min-width:620px}th{text-align:left;background:#eef5f4;font-size:12px;color:#356166;padding:12px}td{padding:12px;border-top:1px solid var(--line);vertical-align:top}tbody tr:nth-child(even){background:#fafcfb}td:first-child{font-weight:600}code{font:12px/1.8 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere;background:#eaf2f1;border-radius:3px;padding:2px 4px}pre{background:#14353e;color:#e1efef;border-radius:0 0 6px 6px;padding:18px 20px;overflow:auto;margin:0 0 18px}pre code{padding:0;background:none;color:inherit;white-space:pre;font-size:12px}.code-label{margin-top:18px;background:#23464e;color:#b9d4d6;font-size:11px;padding:6px 20px;border-radius:6px 6px 0 0}.tools{display:flex;gap:12px;align-items:center;margin-bottom:20px;font-size:13px}button{font:inherit;border:1px solid #b6cccd;background:white;color:#28585b;padding:7px 12px;border-radius:5px;cursor:pointer}.note{font-size:12px;color:var(--muted);margin-top:20px}a:focus-visible,button:focus-visible{outline:3px solid #c8983c;outline-offset:3px}@media(max-width:1000px){aside{width:220px}main{margin-left:220px;padding:24px}article{padding:26px}h1{font-size:29px}}@media(max-width:760px){aside{position:static;width:auto;padding:18px 20px}aside h2{margin:7px 0}nav{display:flex;overflow:auto;gap:8px}nav a{flex:none;max-width:230px;white-space:nowrap}aside .note{display:none}main{margin:0;padding:16px 12px}article{padding:24px 18px}h1{font-size:27px}h2{font-size:22px}}@media print{@page{size:A4;margin:15mm}aside,.tools{display:none}main{margin:0;max-width:none;padding:0}article{border:0;padding:0}body{background:white;font-size:10pt}h1{font-size:22pt}h2{font-size:16pt;break-after:avoid}h3{break-after:avoid}.tablewrap{overflow:visible}table{min-width:0;font-size:8pt}th,td{padding:7px}tr{break-inside:avoid}thead{display:table-header-group}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eff4f4;color:var(--ink)}pre code{white-space:pre-wrap}a{color:var(--teal)}}
</style></head><body><aside><div class="brand">LC CONTROL / ENGINEERING</div><h2>边缘液冷智控<br>产品与代码手册</h2><nav>__NAV__</nav><p class="note">v__VERSION__ · 2026.09.14<br>厂家事实 / 当前实现 / 工程建议<br>离线可读 · 支持打印</p></aside><main><div class="tools"><a href="../index.html">← 产品工程入口</a><a href="../README.md">快速运行指南</a><button type="button" onclick="window.print()">打印 / PDF</button></div><article>__BODY__</article></main></body></html>'''
    target=SOURCE.with_suffix('.html')
    # 版本取自包元信息；完整内容只来自 Markdown，避免生成旧版正文。
    page=page.replace('__VERSION__',VERSION)
    page=page.replace('2026.09.14',reviewed.replace('-','.'))
    target.write_text(page.replace('__NAV__',navigation).replace('__BODY__',body),encoding='utf-8')
    # 保留用户已收藏的根目录文件路径，仅跳转，不再维护第二份设计正文。
    destination='lc-control/docs/'+quote(target.name)
    entry='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0;url=__DEST__">
<title>AIDC 液冷智控软件 · 当前使用手册</title></head><body>
<p>产品设计、设备接入、算法、代码与验证说明已合并为一份当前手册。</p>
<p><a href="__DEST__">打开产品与使用手册</a></p></body></html>'''
    (ROOT.parent/'AIDC液冷软件产品设计与验证方案.html').write_text(
        entry.replace('__DEST__',destination),encoding='utf-8')
    print(target)


if __name__=='__main__':main()
