"""One integrated plan for walls, complete facade, openings and lintels."""
from __future__ import annotations

from collections import Counter
from html import escape
import json
from pathlib import Path

from shapely.geometry import Polygon

from .plan_geometry import parts, shape_of


COLORS={"walls":"#7D8990","columns":"#253C49","facade_glazing":"#00AFC7",
        "facade_frame":"#D58B25","facade_return":"#D48C96","shaft":"#D93C4C",
        "slab_opening":"#A85EC4","door":"#2384B3","unresolved":"#E58428"}
COLORS["stair_review"]="#BA8A35"
COLORS['stair_opening']='#7358A1'


def _records(result):
    records=[]
    def add(group,shape,identity,color,**extra):
        records.append({"group":group,"footprint":shape,"id":identity,"color":color,**extra})
    for row in result.get("wall_materials_at_door_level",result["wall_materials"]):
        add("walls",row["footprint"],row["id"],COLORS["walls"])
    for row in result["columns"]:
        add("columns",row["footprint"],row["id"],COLORS["columns"])
    for system in result["facade_systems"]:
        for row in system["sections"]:
            group={"glazing":"facade_glazing","frame":"facade_frame","opaque_return":"facade_return"}[row["semantic_subrole"]]
            add(group,row["footprint"],row["id"],COLORS[group])
    slab=result.get("slab_openings",{})
    for row in slab.get("elevator_shafts",[]):
        add("shaft",row["footprint"],row["id"],COLORS["shaft"])
    for row in slab.get("canonical_symbol_cut_regions",[]):
        add("slab_opening",row["footprint"],row["id"],COLORS["slab_opening"])
    for row in slab.get('staircase_openings',[]):
        add('stair_opening',row['footprint'],row['id'],COLORS['stair_opening'])
    for row in slab.get("stair_boundary_review_requests",[]):
        x0,y0,x1,y1=row["locator_bounds_mm"]
        add("stair_review",{"outer":[[x0,y0],[x1,y0],[x1,y1],[x0,y1]],"holes":[]},
            row["id"],COLORS["stair_review"],outline=True)
    for row in result.get("door_assemblies",[]):
        if row.get("lintel_footprint"):
            add("door",row["lintel_footprint"],row["id"],COLORS["door"],outline=True)
        elif row.get("source_bbox_mm"):
            x0,y0,x1,y1=row["source_bbox_mm"]
            add("unresolved",{"outer":[[x0,y0],[x1,y0],[x1,y1],[x0,y1]],"holes":[]},
                row["id"],COLORS["unresolved"],outline=True)
    return records


def write_recognition_review(result,output):
    output=Path(output)
    (output/"recognition.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    records=_records(result)
    shapes=[shape_of(row)for row in records]
    if shapes:
        x0=min(p.bounds[0]for p in shapes);y0=min(p.bounds[1]for p in shapes)
        x1=max(p.bounds[2]for p in shapes);y1=max(p.bounds[3]for p in shapes)
    else:
        x0,y0,x1,y1=0,0,1,1
    width,height=1500,1250
    scale=min((width-120)/max(x1-x0,1),(height-180)/max(y1-y0,1))
    def ring(points):
        return "M "+" L ".join(f"{60+(p[0]-x0)*scale:.3f},{height-90-(p[1]-y0)*scale:.3f}"for p in points)+" Z"
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
         '<rect width="100%" height="100%" fill="#FAFBFC"/>',
         '<text x="60" y="40" font-family="Microsoft JhengHei,sans-serif" font-size="24" fill="#243947">自動辨識重跑｜完整帷幕、牆柱、樓板孔及過梁平面</text>',
         '<text x="60" y="68" font-family="Microsoft JhengHei,sans-serif" font-size="15" fill="#677B87">候選平面；藍線為依宿主牆面推導的過梁範圍，橘框為仍待辨明的門／窗組。</text>']
    for row in records:
        paths=" ".join(ring(points)for points in [row["footprint"]["outer"],*row["footprint"].get("holes",[])])
        opacity=.3 if row["group"]in{"shaft","slab_opening"}else 1
        fill="none"if row.get("outline")else row["color"]
        stroke_width=1.2 if row.get("outline")else .4
        svg.append(f'<path data-layer="{row["group"]}" d="{paths}" fill="{fill}" fill-opacity="{opacity}" stroke="{row["color"]}" stroke-width="{stroke_width}" fill-rule="evenodd"><title>{escape(row["id"])}</title></path>')
    svg.append('</svg>')
    content="\n".join(svg)
    (output/"recognition-plan.svg").write_text(content,encoding="utf-8")
    labels={"walls":"牆體","columns":"柱","facade_glazing":"帷幕玻璃","facade_frame":"帷幕框料",
            "facade_return":"帷幕轉折／不透光部","shaft":"電梯井整孔","slab_opening":"樓板符號孔域",
            "door":"門洞／過梁平面","unresolved":"門窗待核","stair_review":"樓梯孔域待核定位",'stair_opening':'樓梯整間含平台孔域'}
    controls=" ".join(f'<label><input checked type="checkbox" data-group="{group}"><i style="background:{color}"></i>{labels[group]}</label>'for group,color in COLORS.items())
    html='<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>DWG 自動辨識檢查</title><style>body{margin:25px;background:#f4f6f7;color:#243947;font:15px "Microsoft JhengHei",sans-serif}label{display:inline-flex;align-items:center;gap:5px;margin:5px 12px 5px 0}i{width:12px;height:12px}svg{width:100%;height:auto;background:white}header{position:sticky;top:0;padding:12px;background:#f4f6f7;z-index:2}</style><header><h2>自動辨識整合檢查</h2><p>可切換構件；游標停留顯示來源追溯用的候選編號。尺寸與未解決項目見 recognition.md。</p>'+controls+'</header>'+content+'<script>document.querySelectorAll("input[data-group]").forEach(input=>input.onchange=()=>document.querySelectorAll(`[data-layer="${input.dataset.group}"]`).forEach(e=>e.style.display=input.checked?"":"none"));</script></html>'
    (output/"recognition.html").write_text(html,encoding="utf-8")
    lines=["# 自動辨識重跑檢查", "", f"來源 SHA256：`{result.get('source_sha256')}`。", "",
           "牆材、完整帷幕、門洞／過梁及樓板孔域來自本次底層辨識，同一份候選資料生成所有檢查圖。", "",
           "![完整整合平面](recognition-plan.svg)", "", "## 自動辨識數量", "", "| 項目 | 數量 |", "|---|---:|"]
    lines += [f"| {escape(str(key))} | {value} |"for key,value in result["counts"].items()]
    lines += ["", "## 門洞與過梁", "", "| 圖塊實例 | 門型 | 完整門扇跨度 mm | 葉片數 | 宿主狀態 |", "|---|---|---:|---:|---|"]
    for door in result.get("door_assemblies",[]):
        lines.append(f"| {door['instance_path']} | {door['opening_type']} | {door.get('leaf_span_mm',0):.2f} | {door.get('leaf_count','—')} | {door.get('host_audit',{}).get('status',door['status'])} |")
    lines += ["", "梁底、梁頂及作用樓板的標高仍須依來源／本案決策綁定。平面辨識通過只代表本圖已找到對應材料與孔界。", "", "## 帷幕來源與閉合", ""]
    for system in result["facade_systems"]:
        lines.append(f"- `{system['instance_path']}`：{system['status']}；{system['counts']}；有紀錄補接 {len(system['repairs'])} 處；{system.get('perimeter_audit',{}).get('status','按既有來源決策保留參考')}。")
    lines += ["", "## 完整稽核", "", "每個材料輪廓、孔界、門扇、宿主尺寸、來源圖層、完整實例 ID、3D 線段與閉合差異保留在 [recognition.json](recognition.json)。", ""]
    lines += ["## 仍需區分的構造資訊", "", "樓梯固定按整間含平台挖空；紫褐色為來源牆內邊及門檻證明的完整孔域。棕色虛框僅標示仍缺精確邊界的位置，不作扣板輪廓，也不再詢問是否保留平台。", "",
              "過梁淨高、構件基底、樓板作用樓層與實際板底標高仍須有依據；牆柱及全高帷幕頂部固定收到當地板底，不另詢問任意牆高或帷幕頂高。", ""]
    if result.get("wall_void_material_interfaces"):
        lines += ["## 內腔與其他來源材料交界", "", "原內圈與實際淨空分別保存；其他來源牆體仍保留，不把它的占位誤報為空氣。", ""]
        lines += [f"- `{row['void_id']}`：原內圈 {row['source_inner_area_mm2']:.3f} mm²，實際淨空 {row['clear_area_mm2']:.3f} mm²；{row['status']}。"for row in result["wall_void_material_interfaces"]]
    (output/"recognition.md").write_text("\n".join(lines),encoding="utf-8")
    files=["recognition.json","recognition.md","recognition-plan.svg","recognition.html"]
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.path import Path as PlotPath
        from matplotlib.patches import PathPatch, Patch
        from shapely.geometry.polygon import orient
        names={f.name for f in font_manager.fontManager.ttflist}
        for name in ("Microsoft JhengHei","Noto Sans CJK TC","Arial Unicode MS"):
            if name in names:
                plt.rcParams["font.family"]=name;break
        plt.rcParams["axes.unicode_minus"]=False
        def draw(ax,limits=None):
            for row in records:
                polygon=orient(shape_of(row),sign=1)
                vertices=[];codes=[]
                for boundary in [polygon.exterior,*polygon.interiors]:
                    p=list(boundary.coords);vertices+=p
                    codes += [PlotPath.MOVETO]+[PlotPath.LINETO]*(len(p)-2)+[PlotPath.CLOSEPOLY]
                opacity=.3 if row["group"]in{"shaft","slab_opening"}else 1
                ax.add_patch(PathPatch(PlotPath(vertices,codes),facecolor="none"if row.get("outline")else row["color"],
                                       edgecolor=row["color"],lw=.6 if row.get("outline")else .25,alpha=opacity,
                                       linestyle="--"if row.get("outline")else"-"))
            ax.set_aspect("equal")
            if limits:
                ax.set_xlim(limits[0],limits[2]);ax.set_ylim(limits[1],limits[3])
            else:
                mx=(x1-x0)*.025;my=(y1-y0)*.025
                ax.set_xlim(x0-mx,x1+mx);ax.set_ylim(y0-my,y1+my)
            ax.set_axis_off()
        fig,ax=plt.subplots(figsize=(16,13),dpi=200,facecolor="#FAFBFC")
        draw(ax)
        ax.set_title("自動辨識重跑｜完整帷幕・牆柱・樓板孔・過梁平面",fontsize=17,pad=18)
        ax.legend(handles=[Patch(facecolor=color,label=labels[group])for group,color in COLORS.items()],loc="lower center",bbox_to_anchor=(.5,-.06),ncol=5,frameon=False,fontsize=9)
        fig.tight_layout();fig.savefig(output/"recognition-plan.png",bbox_inches="tight");plt.close(fig)
        files.append("recognition-plan.png")
        openings=[shape_of(row)for row in result.get("slab_openings",{}).get("elevator_shafts",[])]
        if openings:
            bounds=(min(s.bounds[0]for s in openings)-2000,min(s.bounds[1]for s in openings)-3000,
                    max(s.bounds[2]for s in openings)+2000,max(s.bounds[3]for s in openings)+2000)
            fig,ax=plt.subplots(figsize=(15,12),dpi=200,facecolor="#FAFBFC");draw(ax,bounds)
            ax.set_title("核心區檢查｜電梯井整孔、樓板符號及門上段",fontsize=16,pad=15)
            fig.tight_layout();fig.savefig(output/"recognition-core.png",bbox_inches="tight");plt.close(fig)
            files.append("recognition-core.png")
    except ImportError:
        pass
    return files
