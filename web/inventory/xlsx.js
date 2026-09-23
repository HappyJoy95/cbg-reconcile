/* 库存盘点 - 最小 xlsx 导出器（零依赖，手写 ZIP + OOXML）
 * 归入 globalThis.IC.xlsx
 * 不压缩（STORE），Excel / WPS 均可正常打开。
 */
globalThis.IC = globalThis.IC || {};
(function () {
  const IC = globalThis.IC;

  /* ---------- XML 转义 ---------- */
  function esc(v) {
    return String(v === null || v === undefined ? '' : v)
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&apos;');
  }

  /* ---------- 列号 ---------- */
  function colName(n) {
    let s = '';
    n += 1;
    while (n > 0) {
      const m = (n - 1) % 26;
      s = String.fromCharCode(65 + m) + s;
      n = Math.floor((n - 1) / 26);
    }
    return s;
  }

  /* ---------- CRC32 ---------- */
  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[i] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  const enc = new TextEncoder();

  function dosDateTime(d) {
    const time = ((d.getHours() & 31) << 11) | ((d.getMinutes() & 63) << 5) | ((d.getSeconds() / 2) & 31);
    const date = (((d.getFullYear() - 1980) & 127) << 9) | (((d.getMonth() + 1) & 15) << 5) | (d.getDate() & 31);
    return { time: time, date: date };
  }

  /** 打包 ZIP（STORE 模式）entries: [{name, data:Uint8Array}] */
  function zip(entries) {
    const now = dosDateTime(new Date());
    const chunks = [];
    const central = [];
    let offset = 0;

    entries.forEach((e) => {
      const nameBytes = enc.encode(e.name);
      const data = e.data;
      const crc = crc32(data);

      const local = new Uint8Array(30 + nameBytes.length);
      const lv = new DataView(local.buffer);
      lv.setUint32(0, 0x04034b50, true); // 签名
      lv.setUint16(4, 20, true); // 版本
      lv.setUint16(6, 0, true); // 标志
      lv.setUint16(8, 0, true); // 压缩方法 = 0 (store)
      lv.setUint16(10, now.time, true);
      lv.setUint16(12, now.date, true);
      lv.setUint32(14, crc, true);
      lv.setUint32(18, data.length, true);
      lv.setUint32(22, data.length, true);
      lv.setUint16(26, nameBytes.length, true);
      lv.setUint16(28, 0, true);
      local.set(nameBytes, 30);

      chunks.push(local, data);

      const cen = new Uint8Array(46 + nameBytes.length);
      const cv = new DataView(cen.buffer);
      cv.setUint32(0, 0x02014b50, true);
      cv.setUint16(4, 20, true);
      cv.setUint16(6, 20, true);
      cv.setUint16(8, 0, true);
      cv.setUint16(10, 0, true);
      cv.setUint16(12, now.time, true);
      cv.setUint16(14, now.date, true);
      cv.setUint32(16, crc, true);
      cv.setUint32(20, data.length, true);
      cv.setUint32(24, data.length, true);
      cv.setUint16(28, nameBytes.length, true);
      cv.setUint16(30, 0, true);
      cv.setUint16(32, 0, true);
      cv.setUint16(34, 0, true);
      cv.setUint16(36, 0, true);
      cv.setUint32(38, 0, true);
      cv.setUint32(42, offset, true);
      cen.set(nameBytes, 46);
      central.push(cen);

      offset += local.length + data.length;
    });

    const centralSize = central.reduce((a, c) => a + c.length, 0);
    const end = new Uint8Array(22);
    const ev = new DataView(end.buffer);
    ev.setUint32(0, 0x06054b50, true);
    ev.setUint16(4, 0, true);
    ev.setUint16(6, 0, true);
    ev.setUint16(8, entries.length, true);
    ev.setUint16(10, entries.length, true);
    ev.setUint32(12, centralSize, true);
    ev.setUint32(16, offset, true);
    ev.setUint16(20, 0, true);

    const all = chunks.concat(central, [end]);
    const total = all.reduce((a, c) => a + c.length, 0);
    const out = new Uint8Array(total);
    let p = 0;
    all.forEach((c) => {
      out.set(c, p);
      p += c.length;
    });
    return out;
  }

  /* ---------- 工作表 XML ---------- */
  function sheetXml(sheet) {
    const rows = sheet.rows || [];
    const widths = sheet.widths || [];
    const out = [];
    out.push('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>');
    out.push(
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0">' +
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
    );
    if (widths.length) {
      out.push('<cols>');
      widths.forEach((w, i) => out.push(`<col min="${i + 1}" max="${i + 1}" width="${w}" customWidth="1"/>`));
      out.push('</cols>');
    }
    out.push('<sheetData>');
    rows.forEach((row, r) => {
      const rn = r + 1;
      out.push(`<row r="${rn}">`);
      row.forEach((cell, c) => {
        if (cell === null || cell === undefined || cell === '') return;
        const ref = colName(c) + rn;
        const style = r === 0 && sheet.headerStyle !== false ? ' s="1"' : '';
        if (typeof cell === 'number' && Number.isFinite(cell)) {
          out.push(`<c r="${ref}"${style}><v>${cell}</v></c>`);
        } else {
          out.push(`<c r="${ref}"${style} t="inlineStr"><is><t xml:space="preserve">${esc(cell)}</t></is></c>`);
        }
      });
      out.push('</row>');
    });
    out.push('</sheetData></worksheet>');
    return out.join('');
  }

  const STYLES =
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
    '<fonts count="2"><font><sz val="11"/><name val="微软雅黑"/></font>' +
    '<font><b/><sz val="11"/><name val="微软雅黑"/></font></fonts>' +
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>' +
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>' +
    '</styleSheet>';

  function safeSheetName(name, used) {
    let n = String(name || 'Sheet')
      .replace(/[\[\]\*\?\/\\:]/g, '_')
      .slice(0, 31);
    if (!n) n = 'Sheet';
    let base = n,
      i = 2;
    while (used.has(n)) {
      const suffix = '(' + i++ + ')';
      n = base.slice(0, 31 - suffix.length) + suffix;
    }
    used.add(n);
    return n;
  }

  /**
   * 生成 xlsx 字节
   * @param {Array<{name:string, rows:Array<Array>, widths?:number[]}>} sheets
   * @returns {Uint8Array}
   */
  function build(sheets) {
    const used = new Set();
    const list = (sheets || []).map((s) => ({
      name: safeSheetName(s.name, used),
      rows: s.rows || [],
      widths: s.widths || [],
    }));
    if (!list.length) list.push({ name: 'Sheet1', rows: [], widths: [] });

    const entries = [];
    const contentTypes = [
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
      '<Default Extension="xml" ContentType="application/xml"/>',
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ];
    list.forEach((s, i) => {
      contentTypes.push(
        `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`
      );
    });
    contentTypes.push('</Types>');

    entries.push({
      name: '[Content_Types].xml',
      data: enc.encode(contentTypes.join('')),
    });
    entries.push({
      name: '_rels/.rels',
      data: enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
          '</Relationships>'
      ),
    });

    const sheetTags = list.map((s, i) => `<sheet name="${esc(s.name)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join('');
    entries.push({
      name: 'xl/workbook.xml',
      data: enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
          `<sheets>${sheetTags}</sheets></workbook>`
      ),
    });

    const rels = list
      .map(
        (s, i) =>
          `<Relationship Id="rId${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`
      )
      .join('');
    entries.push({
      name: 'xl/_rels/workbook.xml.rels',
      data: enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          rels +
          `<Relationship Id="rId${list.length + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>` +
          '</Relationships>'
      ),
    });
    entries.push({ name: 'xl/styles.xml', data: enc.encode(STYLES) });

    list.forEach((s, i) => {
      entries.push({ name: `xl/worksheets/sheet${i + 1}.xml`, data: enc.encode(sheetXml(s)) });
    });

    return zip(entries);
  }

  /** 触发浏览器下载 */
  function download(filename, bytes, mime) {
    const blob = new Blob([bytes], {
      type: mime || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1500);
  }

  IC.xlsx = { build, download, zip, crc32, esc, colName };
})();
