"""Export the allocated planar fabric; uses the already selected Program IR."""
from pathlib import Path


def plot_layout(program, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    p=program.physical_layout
    if p is None:return
    fig,ax=plt.subplots(figsize=(10,6))
    ax.add_patch(Rectangle((0,0),p['width'],p['height'],facecolor='#f8fafc',edgecolor='#334155'))
    for s in p['sites']:
        x,y,w,h=s['rect']
        ax.add_patch(Rectangle((x,y),w,h,facecolor='#38a169' if s['enabled'] else '#cbd5e1',edgecolor='#166534'))
        ax.text(x+w/2,y+h/2,f"F{s['id']}",ha='center',va='center',fontsize=7)
        bx,by=s['buffer'];ax.add_patch(Rectangle((bx,by),1,1,facecolor='#f6ad55'))
    if p['layers']:
        for d in p['layers'][0]:
            x,y=d['origin'];w,h=d['size']
            ax.add_patch(Rectangle((x,y),w,h,facecolor='#90cdf4',edgecolor='#2b6cb0'))
            ax.text(x+w/2,y+h/2,f"C{d['macro']}",ha='center',va='center',fontsize=7)
    for q,(x,y) in p['initial']:
        ax.plot(x+.5,y+.5,'o',color='#1e3a8a',markersize=3)
    if p['injections']:
        first=min(i[6] for i in p['injections'])
        for i in p['injections']:
            if i[6]!=first:continue
            route=p['routes'][i[5]]
            ax.plot([xy[0]+.5 for xy in route],[xy[1]+.5 for xy in route],linewidth=1,alpha=.8)
    ax.set(xlim=(-1,p['width']+1),ylim=(-1,p['height']+1),xlabel='I (logical tiles)',ylabel='J (logical tiles)',
           title=f"{program.factory_mode}: {p['width']} × {p['height']} tiles; peak injections = {p['peak_concurrent_injections']}")
    ax.set_aspect('equal')
    fig.tight_layout();fig.savefig(Path(path),dpi=160);plt.close(fig)
