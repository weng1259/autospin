#!/usr/bin/env python3
"""生成采购清单 Word 文档"""

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()

# 页面设置
for section in doc.sections:
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)

# 标题
title = doc.add_heading("智能旋涂仪 — 待采购清单", level=1)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER

doc.add_paragraph(f"生成日期：2026-03-17")
doc.add_paragraph("")

# 采购项目数据
items = [
    ("1", "万用表", "数字万用表", "~30元", "万用表 数字", "建议尽快买，通电前检查线路", True),
    ("2", "正泰继电器+底座", "NXJ/2Z 24VDC（必须24VDC！）", "~15元", "正泰 NXJ/2Z 24VDC 底座", "测Z轴刹车时需要", True),
    ("3", "DB9免焊接头（母头）×3", "DB9 母头 免焊螺丝端子", "~12元(3个)", "DB9 免焊接头 母头", "接编码器必须，不买没法接线", True),
    ("4", "三芯电源线", "3×1.0mm² 国标三脚插头\n散线端 1.5米", "~8元", "三芯电源线 带插头 散线", "通电必须，电源AC输入", True),
    ("5", "内六角扳手套装", "公制 1.5~6mm", "~8元", "内六角扳手套装 公制", "随时需要", False),
    ("6", "十字螺丝刀", "PH2", "~5元", "十字螺丝刀", "备用", False),
    ("7", "电子线", "20AWG 红+黑+彩色", "~15元", "电子线 20AWG", "正式走线整理时", False),
    ("8", "螺丝 M4×16", "内六角不锈钢 10颗装", "~3元", "M4×16 内六角 不锈钢", "固定电机", False),
    ("9", "螺丝 M4×20", "内六角不锈钢 10颗装", "~3元", "M4×20 内六角 不锈钢", "固定电机备用", False),
    ("10", "螺丝 M5×16", "内六角不锈钢 10颗装", "~3元", "M5×16 内六角 不锈钢", "固定电机", False),
    ("11", "螺丝 M5×20", "内六角不锈钢 10颗装", "~3元", "M5×20 内六角 不锈钢", "固定电机备用", False),
    ("12", "扎带", "尼龙 3×150mm 100根", "~5元", "尼龙扎带 3×150", "整理线束", False),
    ("13", "电工胶带", "黑色", "~3元", "电工胶带", "绝缘固定", False),
]

# 表格
doc.add_heading("待采购清单（淘宝）", level=2)
doc.add_paragraph("总计约 101 元。标★的为优先购买项。")

table = doc.add_table(rows=1, cols=6)
table.style = "Table Grid"
table.alignment = WD_TABLE_ALIGNMENT.CENTER

# 表头
headers = ["序号", "名称", "规格", "参考价", "淘宝搜索关键词", "备注"]
header_row = table.rows[0]
for i, h in enumerate(headers):
    cell = header_row.cells[i]
    cell.text = h
    for p in cell.paragraphs:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in p.runs:
            run.bold = True
            run.font.size = Pt(10)

# 数据行
for seq, name, spec, price, search, note, priority in items:
    row = table.add_row()
    display_name = f"★ {name}" if priority else name
    display_note = f"⚠ {note}" if priority else note
    values = [seq, display_name, spec, price, search, display_note]
    for i, val in enumerate(values):
        cell = row.cells[i]
        cell.text = val
        for p in cell.paragraphs:
            for run in p.runs:
                run.font.size = Pt(9)
                if priority and i == 1:
                    run.bold = True
                    run.font.color.rgb = RGBColor(0xCC, 0x00, 0x00)

# 列宽调整
widths = [Cm(1.2), Cm(3.5), Cm(4), Cm(2), Cm(4.5), Cm(3.5)]
for row in table.rows:
    for i, w in enumerate(widths):
        row.cells[i].width = w

doc.add_paragraph("")

# 注意事项
doc.add_heading("采购注意事项", level=2)
warnings = [
    "继电器必须买 24VDC 线圈版本，不能买 220VAC 的！",
    'DB9 免焊接头要买"母头"（和电机的公头对插）',
    "三芯电源线要带地线（三脚插头），两脚的不能用",
    "螺丝 M4 和 M5 各买两种长度（16mm 和 20mm），试哪个合适",
    "万用表建议优先买，到货后先检查所有线路再通电",
]
for w in warnings:
    p = doc.add_paragraph(w, style="List Bullet")
    for run in p.runs:
        run.font.size = Pt(10)

# 已到货清单（供参考）
doc.add_paragraph("")
doc.add_heading("已到货清单（仅供参考）", level=2)

arrived_table = doc.add_table(rows=1, cols=3)
arrived_table.style = "Table Grid"
arrived_table.alignment = WD_TABLE_ALIGNMENT.CENTER

arrived_headers = ["名称", "来源", "用途"]
for i, h in enumerate(arrived_headers):
    cell = arrived_table.rows[0].cells[i]
    cell.text = h
    for p in cell.paragraphs:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in p.runs:
            run.bold = True
            run.font.size = Pt(10)

arrived_items = [
    ("24V 15A 开关电源", "京东", "给驱动器+继电器+传感器统一供电"),
    ("剥线钳", "京东", "剥电线皮"),
    ("小一字螺丝刀 3mm", "京东", "拧驱动器绿色接线端子"),
    ("杜邦线 公对母 40P", "京东", "Arduino接驱动器信号线"),
    ("Arduino Mega 2560（CH340）", "京东", "主控制器"),
]
for name, source, usage in arrived_items:
    row = arrived_table.add_row()
    for i, val in enumerate([name, source, usage]):
        cell = row.cells[i]
        cell.text = val
        for p in cell.paragraphs:
            for run in p.runs:
                run.font.size = Pt(9)

# 保存
output_path = "/Users/kevin/Code/智能旋涂仪/采购清单.docx"
doc.save(output_path)
print(f"已生成: {output_path}")
