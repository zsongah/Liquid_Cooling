"""无 matplotlib 的 macOS 宿主备用绘图：只用已保存序列生成 PNG / SVG。

Pillow 为现有本机依赖，字体/矢量基本形状复用项目图解工具。
不运行 FMU、不加载设备驱动；阶梯目标不插值为未来已知输入。
"""
from build_architecture import Diagram, MUTED, TEAL


def line(d,points,color,width=2,dashed=False):
    # 虚线使用相同的小段几何，确保 PNG 与 SVG 一致。
    if dashed:
        for a,b in zip(points,points[1:]):
            import math
            length=math.hypot(b[0]-a[0],b[1]-a[1])
            for start in range(0,int(length),12):
                end=min(start+6,length)
                line(d,[(a[0]+(b[0]-a[0])*start/length,a[1]+(b[1]-a[1])*start/length),
                        (a[0]+(b[0]-a[0])*end/length,a[1]+(b[1]-a[1])*end/length)],color,width)
        return
    d.draw.line(points,fill=color,width=width)
    d.svg.append('<polyline points="'+' '.join(f'{x},{y}' for x,y in points)
                 +f'" fill="none" stroke="{color}" stroke-width="{width}"/>')


def render(data,output):
    for kind,title,unit,lo,hi,yticks in [
        ('flow','需求流量与实际流量','kg/s',9,18,[10,12,14,16,18]),
        ('dp','策略压差、实际下发目标与实测压差','kPa',160,270,[160,180,200,220,240,260])]:
        d=Diagram(1800,920,title)
        d.text(60,35,title,43,bold=True)
        d.text(62,100,'原闭环日志补图 · 固定参数与在线校准分别对照 · 非新 FMU 运行结果',25,MUTED)
        for i,p in enumerate(data):
            left=100+i*875;top=235;w=750;h=470
            d.text(left,163,'固定参数算法' if p['variant']=='fixed' else '在线校准算法',31,bold=True)
            d.text(left,204,unit,23,MUTED)
            project=lambda x,y:(left+x/60*w,top+h-(y-lo)/(hi-lo)*h)
            for y in yticks:
                pos=project(0,y)[1];line(d,[(left,pos),(left+w,pos)],'#DFE6EB',1)
                d.text(left-65,pos-11,str(y),22,MUTED)
            for t in range(0,61,10):
                x=project(t,lo)[0];line(d,[(x,top),(x,top+h)],'#E5EBEE',1)
                d.text(x-12,top+h+18,str(t),22,MUTED)
            line(d,[(left,top),(left,top+h),(left+w,top+h)],MUTED,2)
            d.text(left+180,top+h+57,'预热结束后的时间（分钟）',24,MUTED)
            series=([(p['tx'],p['demand'],'#C48A36',True,False),
                     (p['times'],p['flows'],TEAL,False,False)] if kind=='flow' else
                    [(p['tx'],p['desired'],'#A6AFB7',True,True),
                     (p['wt'],p['wv'],'#C48A36',True,False),
                     (p['times'],p['dp'],TEAL,False,False)])
            for xs,ys,color,step,dashed in series:
                pts=[]
                for j,(x,y) in enumerate(zip(xs,ys)):
                    if step and j:pts.append(project(x,ys[j-1]))
                    pts.append(project(x,y))
                line(d,pts,color,3,dashed)
        labels=([('需求流量（限幅后）','#C48A36'),('实际流量',TEAL)] if kind=='flow' else
                [('限速前的策略目标','#A6AFB7'),('实际下发目标','#C48A36'),('实际压差',TEAL)])
        for j,(label,color) in enumerate(labels):
            x=115+j*560;line(d,[(x,822),(x+65,822)],color,4,color=='#A6AFB7')
            d.text(x+80,806,label,26,MUTED)
        note=('需求流量是算法内部中间量，本轮实际写入的是压差目标。'
              if kind=='flow' else '灰线已受设备上下限约束；橙线另外受每 60 秒最多 3 kPa 的变化限制。')
        d.text(63,865,note,25,MUTED)
        stem=kind+'_tracking'
        d.image.save(output/(stem+'.png'))
        (output/(stem+'.svg')).write_text('\n'.join(d.svg+['</svg>']),encoding='utf-8')
