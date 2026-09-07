"""Printed BOM page labels; preserve paper size, margins and scaling."""
from xml.dom import minidom

STYLE='urn:oasis:names:tc:opendocument:xmlns:style:1.0'
TEXT='urn:oasis:names:tc:opendocument:xmlns:text:1.0'
OFFICE='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
FO='urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0'


def apply_page_labels(data):
    doc=minidom.parseString(data)
    root=doc.documentElement
    for prefix,uri in [('style',STYLE),('text',TEXT),('office',OFFICE),('fo',FO)]:
        root.setAttribute('xmlns:'+prefix,uri)

    def element(uri,name,attrs=()):
        node=doc.createElementNS(uri,name)
        for ns,key,value in attrs:node.setAttributeNS(ns,key,value)
        return node

    masters=doc.getElementsByTagNameNS(STYLE,'master-page')
    if not masters:
        doc.unlink()
        return data
    names={n.getAttributeNS(STYLE,'name') for n in doc.getElementsByTagNameNS(STYLE,'style')}
    style_name='FineBOMPageNumber'
    index=0
    while style_name in names:
        index+=1
        style_name=f'FineBOMPageNumber{index}'
    containers=doc.getElementsByTagNameNS(OFFICE,'styles')
    if containers:
        container=containers[0]
    else:
        container=element(OFFICE,'office:styles')
        anchor=next((n for n in root.childNodes if n.namespaceURI==OFFICE and n.localName in ('automatic-styles','master-styles')),None)
        root.insertBefore(container,anchor)
    para_style=element(STYLE,'style:style',[(STYLE,'style:name',style_name),(STYLE,'style:family','paragraph')])
    para_style.appendChild(element(STYLE,'style:paragraph-properties',[(FO,'fo:text-align','center')]))
    container.appendChild(para_style)
    for master in masters:
        # Clear all variants; explicit empty disabled headers override defaults.
        for child in list(master.childNodes):
            if child.namespaceURI==STYLE and child.localName in (
                'header','header-left','header-first','footer','footer-left','footer-first'):
                master.removeChild(child)
        for variant in ('header','header-left','header-first'):
            master.appendChild(element(STYLE,'style:'+variant,[(STYLE,'style:display','false')]))
        for variant in ('footer','footer-left','footer-first'):
            footer=element(STYLE,'style:'+variant,[(STYLE,'style:display','true')])
            paragraph=element(TEXT,'text:p',[(TEXT,'text:style-name',style_name)])
            paragraph.appendChild(doc.createTextNode('Page '))
            page=element(TEXT,'text:page-number',[(TEXT,'text:select-page','current')])
            page.appendChild(doc.createTextNode('1'))
            paragraph.appendChild(page)
            paragraph.appendChild(doc.createTextNode(' from '))
            total=element(TEXT,'text:page-count')
            total.appendChild(doc.createTextNode('1'))
            paragraph.appendChild(total)
            footer.appendChild(paragraph)
            master.appendChild(footer)
    result=doc.toxml(encoding='utf-8')
    doc.unlink()
    return result
