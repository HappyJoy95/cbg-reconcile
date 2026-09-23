/**
 * Main Application Logic
 */
(function() {
    // ===== Tab Switching =====
    var sidebarBtns = document.querySelectorAll('.sidebar-nav button');

    function activateToolTab(tabId) {
        if (!tabId) return;
        sidebarBtns.forEach(function(b) { b.classList.toggle('active', b.dataset.tab === tabId); });
        document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
        var panel = document.getElementById(tabId + 'Panel');
        if (panel) panel.classList.add('active');
        var previewPanel = document.getElementById('previewPanel');
        var ptPrev = document.getElementById('ptPreviewSection');
        var bdPrev = document.getElementById('bdPreviewSection');
        if (!previewPanel || !ptPrev || !bdPrev) return;
        ptPrev.style.display = 'none';
        bdPrev.style.display = 'none';
        if (tabId === 'pricetag' && typeof ptState !== 'undefined' && ptState.resultCanvases.length > 0) {
            previewPanel.style.display = ''; ptPrev.style.display = '';
        } else if (tabId === 'badge' && typeof badgeState !== 'undefined' && badgeState.badges.length > 0) {
            previewPanel.style.display = ''; bdPrev.style.display = '';
        } else {
            previewPanel.style.display = 'none';
        }
    }
    var initTab = (document.documentElement.dataset && document.documentElement.dataset.toolTab) || 'pricetag';
    window.addEventListener('message', function(e) {
        if (e.origin && e.origin !== location.origin) return;
        var d = e.data || {};
        if (d.ic === 'tools-tab') activateToolTab(d.name === 'badge' ? 'badge' : 'pricetag');
    });
    setTimeout(function() { activateToolTab(initTab); }, 0);
    var panels = document.querySelectorAll('.tab-panel');
    var previewPanel = document.getElementById('previewPanel');
    var ptPreviewSection = document.getElementById('ptPreviewSection');
    var bdPreviewSection = document.getElementById('bdPreviewSection');

    sidebarBtns.forEach(function(btn) {
        btn.addEventListener('click', function() {
            sidebarBtns.forEach(function(b) { b.classList.remove('active'); });
            panels.forEach(function(p) { p.classList.remove('active'); });
            btn.classList.add('active');
            var tabId = btn.dataset.tab;
            document.getElementById(tabId + 'Panel').classList.add('active');

            // Show/hide preview panel and correct preview section
            ptPreviewSection.style.display = 'none';
            bdPreviewSection.style.display = 'none';
            if (tabId === 'pricetag' && ptState.resultCanvases.length > 0) {
                previewPanel.style.display = '';
                ptPreviewSection.style.display = '';
            } else if (tabId === 'badge' && badgeState.badges.length > 0) {
                previewPanel.style.display = '';
                bdPreviewSection.style.display = '';
            } else {
                previewPanel.style.display = 'none';
            }
        });
    });

    // ===== Layout Tool Module =====
    var ptState = {
        items: [],
        resultCanvases: [],
        cutLineCanvases: [],
        pageLayouts: [],
        currentPage: 0
    };

    var pt = {
        dropZone: document.getElementById('dropZone'),
        fileInput: document.getElementById('fileInput'),
        fileList: document.getElementById('fileList'),
        gapX: document.getElementById('gapX'),
        gapY: document.getElementById('gapY'),
        scaleRange: document.getElementById('scaleRange'),
        scaleValue: document.getElementById('scaleValue'),
        batchQuantity: document.getElementById('batchQuantity'),
        processBtn: document.getElementById('processBtn'),
        clearBtn: document.getElementById('clearBtn'),
        previewSection: document.getElementById('ptPreviewSection'),
        contentCanvas: document.getElementById('previewContentCanvas'),
        cutLineCanvas: document.getElementById('previewCutLineCanvas'),
        previewPagination: document.getElementById('previewPagination'),
        prevPage: document.getElementById('prevPage'),
        nextPage: document.getElementById('nextPage'),
        pageInfo: document.getElementById('pageInfo'),
        resultInfo: document.getElementById('resultInfo'),
        downloadBtn: document.getElementById('downloadBtn'),
        loadingSection: document.getElementById('loadingSection'),
        loadingText: document.getElementById('loadingText')
    };

    function ptInit() {
        pt.fileInput.addEventListener('change', function(e) {
            ptAddFiles(e.target.files);
            pt.fileInput.value = '';
        });

        pt.dropZone.addEventListener('dragover', function(e) {
            e.preventDefault();
            pt.dropZone.classList.add('dragover');
        });
        pt.dropZone.addEventListener('dragleave', function() {
            pt.dropZone.classList.remove('dragover');
        });
        pt.dropZone.addEventListener('drop', function(e) {
            e.preventDefault();
            pt.dropZone.classList.remove('dragover');
            ptAddFiles(e.dataTransfer.files);
        });

        pt.processBtn.addEventListener('click', ptProcessImages);
        pt.clearBtn.addEventListener('click', ptClearAll);
        pt.downloadBtn.addEventListener('click', ptDownloadResult);

        pt.fileList.addEventListener('click', function(e) {
            var index = Number(e.target.dataset.index);
            if (Number.isNaN(index)) return;
            if (e.target.classList.contains('qty-minus')) ptChangeQuantity(index, -1);
            else if (e.target.classList.contains('qty-plus')) ptChangeQuantity(index, 1);
            else if (e.target.classList.contains('file-remove')) ptRemoveItem(index);
        });

        pt.gapX.addEventListener('change', ptAutoUpdatePreview);
        pt.gapY.addEventListener('change', ptAutoUpdatePreview);
        document.querySelectorAll('input[name="lineStyle"]').forEach(function(radio) {
            radio.addEventListener('change', ptRegenerateCutLines);
        });
        pt.scaleRange.addEventListener('input', function() {
            pt.scaleValue.textContent = pt.scaleRange.value + '%';
        });
        pt.scaleRange.addEventListener('change', ptAutoUpdatePreview);

        pt.batchQuantity.addEventListener('click', function(e) {
            var qty = Number(e.target.dataset.qty);
            if (Number.isNaN(qty)) return;
            ptState.items.forEach(function(item) { item.quantity = qty; });
            ptRenderFileList();
        });

        pt.prevPage.addEventListener('click', function() {
            if (ptState.currentPage > 0) {
                ptState.currentPage--;
                ptShowPage(ptState.currentPage);
            }
        });
        pt.nextPage.addEventListener('click', function() {
            if (ptState.currentPage < ptState.resultCanvases.length - 1) {
                ptState.currentPage++;
                ptShowPage(ptState.currentPage);
            }
        });

        document.querySelectorAll('input[name="exportFormat"]').forEach(function(radio) {
            radio.addEventListener('change', ptUpdateDownloadText);
        });

        ptRenderFileList();
    }

    function ptShowPage(index) {
        var content = ptState.resultCanvases[index];
        var cutlines = ptState.cutLineCanvases[index];

        pt.contentCanvas.width = content.width;
        pt.contentCanvas.height = content.height;
        pt.contentCanvas.getContext('2d').drawImage(content, 0, 0);

        if (cutlines) {
            pt.cutLineCanvas.width = cutlines.width;
            pt.cutLineCanvas.height = cutlines.height;
            pt.cutLineCanvas.getContext('2d').drawImage(cutlines, 0, 0);
            pt.cutLineCanvas.style.display = '';
        } else {
            pt.cutLineCanvas.style.display = 'none';
        }

        pt.pageInfo.textContent = (index + 1) + ' / ' + ptState.resultCanvases.length;
        pt.prevPage.disabled = index === 0;
        pt.nextPage.disabled = index === ptState.resultCanvases.length - 1;
    }

    function ptRegenerateCutLines() {
        if (ptState.pageLayouts.length === 0) return;
        var gapX = (pt.gapX.value === '' ? 0.2 : Number(pt.gapX.value));
        var gapY = (pt.gapY.value === '' ? 0.2 : Number(pt.gapY.value));
        var lineStyle = document.querySelector('input[name="lineStyle"]:checked').value;
        var scale = parseInt(pt.scaleRange.value) / 100;

        ptState.cutLineCanvases = [];
        for (var i = 0; i < ptState.pageLayouts.length; i++) {
            ptState.cutLineCanvases.push(
                LayoutEngine.renderCutLines(ptState.pageLayouts[i], gapX, gapY, scale, lineStyle)
            );
        }
        ptShowPage(ptState.currentPage);
    }

    function ptAutoUpdatePreview() {
        if (ptState.resultCanvases.length > 0) ptProcessImages();
    }

    function ptAddFiles(fileList) {
        Array.from(fileList).forEach(function(file) {
            var isPDF = file.type === 'application/pdf';
            var isImage = file.type.startsWith('image/');
            if (!isPDF && !isImage) return;

            var item = { file: file, image: null, size: null, widthMm: null, heightMm: null, quantity: 1, status: 'parsing' };
            ptState.items.push(item);
            ptRenderFileList();

            if (isPDF) {
                PDFParser.convertToImage(file).then(function(result) {
                    item.image = result.canvas;
                    item.size = result.size;
                    item.widthMm = result.size.widthMm;
                    item.heightMm = result.size.heightMm;
                    item.status = 'ready';
                    ptRenderFileList();
                    ptUpdateProcessButton();
                }).catch(function(error) {
                    console.error('解析PDF失败:', error);
                    item.status = 'error';
                    ptRenderFileList();
                    ptUpdateProcessButton();
                });
            } else {
                var reader = new FileReader();
                reader.onload = function(e) {
                    var img = new Image();
                    img.onload = function() {
                        var canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth;
                        canvas.height = img.naturalHeight;
                        canvas.getContext('2d').drawImage(img, 0, 0);
                        item.image = canvas;
                        var sizeInfo = ImageProcessor.detectSizeFromPixels(img.naturalWidth, img.naturalHeight);
                        item.size = sizeInfo;
                        item.widthMm = sizeInfo.widthMm;
                        item.heightMm = sizeInfo.heightMm;
                        item.status = 'ready';
                        ptRenderFileList();
                        ptUpdateProcessButton();
                    };
                    img.src = e.target.result;
                };
                reader.readAsDataURL(file);
            }
        });
    }

    function ptChangeQuantity(index, delta) {
        ptState.items[index].quantity = Math.max(1, ptState.items[index].quantity + delta);
        ptRenderFileList();
    }

    function ptRemoveItem(index) {
        ptState.items.splice(index, 1);
        ptRenderFileList();
        ptUpdateProcessButton();
    }

    function ptRenderFileList() {
        if (ptState.items.length === 0) {
            pt.fileList.innerHTML = '<div class="empty-list">还没有上传文件</div>';
            pt.batchQuantity.style.display = 'none';
            return;
        }
        pt.batchQuantity.style.display = 'flex';
        pt.fileList.innerHTML = ptState.items.map(function(item, index) {
            var status = item.status === 'parsing' ? '正在解析...' : item.status === 'error' ? '解析失败' : PDFParser.getSizeLabel(item.size);
            var statusClass = item.status === 'error' ? 'file-status error' : 'file-status';
            var fileName = item.fileName || (item.file ? item.file.name : '工牌图片');
            return '<div class="file-row">' +
                '<div class="file-main">' +
                    '<div class="file-name">' + escapeHtml(fileName) + '</div>' +
                    '<div class="' + statusClass + '">' + status + '</div>' +
                '</div>' +
                '<div class="quantity-controls">' +
                    '<button type="button" class="qty-btn qty-minus" data-index="' + index + '">-</button>' +
                    '<span class="qty-value">' + item.quantity + '</span>' +
                    '<button type="button" class="qty-btn qty-plus" data-index="' + index + '">+</button>' +
                '</div>' +
                '<button type="button" class="file-remove" data-index="' + index + '">删除</button>' +
            '</div>';
        }).join('');
    }

    function ptUpdateProcessButton() {
        pt.processBtn.disabled = ptState.items.filter(function(item) { return item.status === 'ready'; }).length === 0;
    }

    function ptBuildLayoutItems() {
        var allItems = [];
        for (var i = 0; i < ptState.items.length; i++) {
            var item = ptState.items[i];
            if (item.status !== 'ready') continue;
            var sizeKey = typeof item.size === 'string' ? item.size : (item.size ? item.size.size : 'small');
            for (var q = 0; q < item.quantity; q++) {
                allItems.push({
                    canvas: item.image,
                    sizeKey: sizeKey,
                    widthMm: item.widthMm || 75,
                    heightMm: item.heightMm || 125
                });
            }
        }
        return allItems;
    }

    async function ptProcessImages() {
        ptShowLoading(true);
        try {
            var allItems = ptBuildLayoutItems();
            if (allItems.length === 0) throw new Error('请至少上传1个可用文件');

            var gapX = (pt.gapX.value === '' ? 0.2 : Number(pt.gapX.value));
            var gapY = (pt.gapY.value === '' ? 0.2 : Number(pt.gapY.value));
            var lineStyle = document.querySelector('input[name="lineStyle"]:checked').value;
            var scale = parseInt(pt.scaleRange.value) / 100;

            var pages = LayoutEngine.buildPages(allItems, gapX, gapY, scale);
            ptState.resultCanvases = [];
            ptState.cutLineCanvases = [];
            ptState.pageLayouts = pages;

            for (var j = 0; j < pages.length; j++) {
                pt.loadingText.textContent = '正在处理第 ' + (j + 1) + ' / ' + pages.length + ' 页...';
                await new Promise(function(r) { setTimeout(r, 0); });
                ptState.resultCanvases.push(
                    LayoutEngine.renderPage(pages[j], gapX, gapY, scale)
                );
                ptState.cutLineCanvases.push(
                    LayoutEngine.renderCutLines(pages[j], gapX, gapY, scale, lineStyle)
                );
            }

            ptState.currentPage = 0;
            ptDisplayResult(ptState.resultCanvases);
        } catch (error) {
            console.error('处理失败:', error);
            alert('处理失败: ' + error.message);
        } finally {
            ptShowLoading(false);
        }
    }

    function ptDisplayResult(canvases) {
        ptShowPage(0);
        pt.previewPagination.style.display = canvases.length > 1 ? 'flex' : 'none';
        pt.resultInfo.innerHTML = '<span>共生成: ' + canvases.length + '页A4</span>';
        var fmt = document.querySelector('input[name="exportFormat"]:checked').value;
        pt.downloadBtn.textContent = canvases.length === 1 ? '下载' + fmt.toUpperCase() : '下载ZIP';
        pt.previewSection.style.display = 'block';
        previewPanel.style.display = '';
    }

    function ptUpdateDownloadText() {
        if (ptState.resultCanvases.length === 0) return;
        var fmt = document.querySelector('input[name="exportFormat"]:checked').value;
        pt.downloadBtn.textContent = ptState.resultCanvases.length === 1 ? '下载' + fmt.toUpperCase() : '下载ZIP';
    }

    async function ptDownloadResult() {
        if (ptState.resultCanvases.length === 0) return;
        var fmt = document.querySelector('input[name="exportFormat"]:checked').value;
        var ext = fmt === 'png' ? 'png' : 'jpg';
        var mime = fmt === 'png' ? 'image/png' : 'image/jpeg';
        var ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);

        function getExportCanvas(i) {
            return LayoutEngine.composite(ptState.resultCanvases[i], ptState.cutLineCanvases[i]);
        }

        if (ptState.resultCanvases.length === 1) {
            var exportCanvas = getExportCanvas(0);
            Exporter.download(exportCanvas.toDataURL(mime, 0.95), 'layout-' + ts + '.' + ext);
            return;
        }
        var zip = new JSZip();
        for (var i = 0; i < ptState.resultCanvases.length; i++) {
            var ec = getExportCanvas(i);
            var blob = await new Promise(function(resolve) {
                ec.toBlob(function(b) { resolve(b); }, mime, 0.95);
            });
            zip.file('layout-page-' + String(i + 1).padStart(2, '0') + '.' + ext, blob);
        }
        var zipBlob = await zip.generateAsync({ type: 'blob' });
        Exporter.downloadBlob(zipBlob, 'layout-' + ts + '.zip');
    }

    function ptClearAll() {
        ptState.items = [];
        ptState.resultCanvases = [];
        ptState.cutLineCanvases = [];
        ptState.pageLayouts = [];
        ptState.currentPage = 0;
        pt.fileInput.value = '';
        ptRenderFileList();
        ptUpdateProcessButton();
        pt.previewSection.style.display = 'none';
        pt.previewPagination.style.display = 'none';
        previewPanel.style.display = 'none';
    }

    function ptShowLoading(show) {
        pt.loadingSection.style.display = show ? 'block' : 'none';
        if (!show) pt.loadingText.textContent = '正在处理...';
        pt.processBtn.disabled = show || ptState.items.filter(function(item) { return item.status === 'ready'; }).length === 0;
    }

    // ===== Badge Module =====
    var badgeState = {
        persons: [],
        badges: [],
        a4Pages: [],
        currentPage: 0,
        mode: 'badges'
    };

    var bd = {
        personList: document.getElementById('badgePersonList'),
        addBtn: document.getElementById('addPersonBtn'),
        generateBtn: document.getElementById('generateBadgeBtn'),
        layoutBtn: document.getElementById('layoutBadgeBtn'),
        importBtn: document.getElementById('importToLayoutBtn'),
        clearBtn: document.getElementById('clearBadgeBtn'),
        previewSection: document.getElementById('bdPreviewSection'),
        previewCanvas: document.getElementById('badgePreviewCanvas'),
        previewPagination: document.getElementById('badgePreviewPagination'),
        prevPage: document.getElementById('badgePrevPage'),
        nextPage: document.getElementById('badgeNextPage'),
        pageInfo: document.getElementById('badgePageInfo'),
        resultInfo: document.getElementById('badgeResultInfo'),
        downloadBtn: document.getElementById('badgeDownloadBtn'),
        loadingSection: document.getElementById('badgeLoadingSection')
    };

    function bdInit() {
        bd.addBtn.addEventListener('click', bdAddPerson);
        bd.generateBtn.addEventListener('click', bdGenerate);
        bd.layoutBtn.addEventListener('click', bdLayout);
        bd.importBtn.addEventListener('click', bdImportToLayoutTool);
        bd.clearBtn.addEventListener('click', bdClearAll);
        bd.downloadBtn.addEventListener('click', bdDownload);

        bd.prevPage.addEventListener('click', function() {
            if (badgeState.currentPage > 0) {
                badgeState.currentPage--;
                bdShowPage(badgeState.currentPage);
            }
        });
        bd.nextPage.addEventListener('click', function() {
            var maxPage = badgeState.mode === 'badges' ? badgeState.badges.length * 2 - 1 : badgeState.a4Pages.length - 1;
            if (badgeState.currentPage < maxPage) {
                badgeState.currentPage++;
                bdShowPage(badgeState.currentPage);
            }
        });

        bdAddPerson();
    }

    function bdAddPerson() {
        var person = {
            id: Date.now(),
            name: '',
            pinyin: '',
            position: '体验顾问',
            qrWecom: null,
            qrHuawei: null
        };
        badgeState.persons.push(person);
        bdRenderList();
    }

    function bdRemovePerson(id) {
        badgeState.persons = badgeState.persons.filter(function(p) { return p.id !== id; });
        bdRenderList();
        bdUpdateGenerateButton();
    }

    function bdUpdateField(id, field, value) {
        var person = badgeState.persons.find(function(p) { return p.id === id; });
        if (person) {
            person[field] = value;
            bdUpdateGenerateButton();
        }
    }

    function bdUpdateQR(id, type, file) {
        var person = badgeState.persons.find(function(p) { return p.id === id; });
        if (!person) return;

        var reader = new FileReader();
        reader.onload = function(e) {
            var img = new Image();
            img.onload = function() {
                var cropped = cropQRCode(img);
                person[type] = cropped;
                bdRenderList();
                bdUpdateGenerateButton();
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    }

    function cropQRCode(img) {
        var canvas = document.createElement('canvas');
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        var ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0);

        var w = canvas.width;
        var h = canvas.height;
        var imageData = ctx.getImageData(0, 0, w, h);
        var data = imageData.data;

        // Find content bounds by scanning for non-white pixels
        var top = h, bottom = 0, left = w, right = 0;
        var threshold = 240;

        for (var y = 0; y < h; y++) {
            for (var x = 0; x < w; x++) {
                var idx = (y * w + x) * 4;
                var r = data[idx], g = data[idx + 1], b = data[idx + 2], a = data[idx + 3];
                if (a < 128) continue; // skip transparent
                if (r < threshold || g < threshold || b < threshold) {
                    if (y < top) top = y;
                    if (y > bottom) bottom = y;
                    if (x < left) left = x;
                    if (x > right) right = x;
                }
            }
        }

        // Add small padding (2% of content size)
        var contentW = right - left;
        var contentH = bottom - top;
        var padX = Math.round(contentW * 0.02);
        var padY = Math.round(contentH * 0.02);
        left = Math.max(0, left - padX);
        top = Math.max(0, top - padY);
        right = Math.min(w - 1, right + padX);
        bottom = Math.min(h - 1, bottom + padY);

        var cropW = right - left + 1;
        var cropH = bottom - top + 1;

        // If cropping doesn't save much, return original
        if (cropW > w * 0.9 && cropH > h * 0.9) return img;

        var croppedCanvas = document.createElement('canvas');
        croppedCanvas.width = cropW;
        croppedCanvas.height = cropH;
        croppedCanvas.getContext('2d').drawImage(canvas, left, top, cropW, cropH, 0, 0, cropW, cropH);
        return croppedCanvas;
    }

    function bdRenderList() {
        if (badgeState.persons.length === 0) {
            bd.personList.innerHTML = '<div class="empty-list">还没有添加人员</div>';
            return;
        }

        bd.personList.innerHTML = badgeState.persons.map(function(person) {
            var qrLabel = person.qrWecom ? '已上传' : '上传二维码';
            var qrClass = person.qrWecom ? 'badge-qr-upload has-image' : 'badge-qr-upload';
            return '<div class="badge-person-row">' +
                '<input type="text" class="badge-input" placeholder="姓名" value="' + escapeHtml(person.name) + '" data-id="' + person.id + '" data-field="name">' +
                '<input type="text" class="badge-input" placeholder="拼音" value="' + escapeHtml(person.pinyin) + '" data-id="' + person.id + '" data-field="pinyin">' +
                '<select class="badge-select" data-id="' + person.id + '" data-field="position">' +
                    Object.keys(BadgeGenerator.POSITIONS).map(function(pos) {
                        return '<option value="' + pos + '"' + (pos === person.position ? ' selected' : '') + '>' + pos + '</option>';
                    }).join('') +
                '</select>' +
                '<div class="' + qrClass + '">' +
                    qrLabel +
                    '<input type="file" accept="image/*" data-id="' + person.id + '" data-qr="qrWecom">' +
                '</div>' +
                '<button type="button" class="badge-remove-btn" data-id="' + person.id + '">&times;</button>' +
            '</div>';
        }).join('');

        bd.personList.querySelectorAll('.badge-input, .badge-select').forEach(function(el) {
            el.addEventListener('change', function() {
                bdUpdateField(Number(el.dataset.id), el.dataset.field, el.value);
            });
            el.addEventListener('input', function() {
                bdUpdateField(Number(el.dataset.id), el.dataset.field, el.value);
            });
        });

        bd.personList.querySelectorAll('input[type="file"]').forEach(function(el) {
            el.addEventListener('change', function(e) {
                if (e.target.files[0]) {
                    bdUpdateQR(Number(el.dataset.id), el.dataset.qr, e.target.files[0]);
                }
            });
        });

        bd.personList.querySelectorAll('.badge-remove-btn').forEach(function(el) {
            el.addEventListener('click', function() {
                bdRemovePerson(Number(el.dataset.id));
            });
        });
    }

    function bdUpdateGenerateButton() {
        var valid = badgeState.persons.filter(function(p) {
            return p.name.trim().length > 0 && p.pinyin.trim().length > 0;
        });
        bd.generateBtn.disabled = valid.length === 0;
        bd.layoutBtn.disabled = badgeState.badges.length === 0;
        bd.importBtn.disabled = badgeState.badges.length === 0;
    }

    async function bdGenerate() {
        var validPersons = badgeState.persons.filter(function(p) {
            return p.name.trim().length > 0 && p.pinyin.trim().length > 0;
        });
        if (validPersons.length === 0) return;

        bd.loadingSection.style.display = 'block';
        bd.generateBtn.disabled = true;

        await new Promise(function(r) { setTimeout(r, 50); });

        try {
            badgeState.badges = [];
            for (var i = 0; i < validPersons.length; i++) {
                var p = validPersons[i];
                var front = BadgeGenerator.renderBadgeFront(p.name, p.pinyin, p.position);
                var back = BadgeGenerator.renderBadgeBack(p.qrWecom, p.qrHuawei);
                badgeState.badges.push({ front: front, back: back, name: p.name });
            }
            badgeState.a4Pages = [];
            badgeState.mode = 'badges';
            badgeState.currentPage = 0;
            bdDisplayBadges();
        } catch (error) {
            console.error('生成工牌失败:', error);
            alert('生成工牌失败: ' + error.message);
        } finally {
            bd.loadingSection.style.display = 'none';
            bdUpdateGenerateButton();
        }
    }

    async function bdLayout() {
        if (badgeState.badges.length === 0) return;

        bd.loadingSection.style.display = 'block';
        bd.layoutBtn.disabled = true;

        await new Promise(function(r) { setTimeout(r, 50); });

        try {
            var allCanvases = [];
            for (var i = 0; i < badgeState.badges.length; i++) {
                allCanvases.push(badgeState.badges[i].front);
                allCanvases.push(badgeState.badges[i].back);
            }
            badgeState.a4Pages = BadgeGenerator.layoutBadgesOnA4(allCanvases);
            badgeState.mode = 'a4';
            badgeState.currentPage = 0;
            bdDisplayA4();
        } catch (error) {
            console.error('排版失败:', error);
            alert('排版失败: ' + error.message);
        } finally {
            bd.loadingSection.style.display = 'none';
            bdUpdateGenerateButton();
        }
    }

    function bdImportToLayoutTool() {
        if (badgeState.badges.length === 0) return;

        var items = [];
        for (var i = 0; i < badgeState.badges.length; i++) {
            var b = badgeState.badges[i];
            items.push({
                file: null,
                fileName: b.name + '-正面',
                image: b.front,
                size: { size: 'badge', widthMm: 54, heightMm: 86 },
                widthMm: 54,
                heightMm: 86,
                quantity: 1,
                status: 'ready'
            });
            items.push({
                file: null,
                fileName: b.name + '-背面',
                image: b.back,
                size: { size: 'badge', widthMm: 54, heightMm: 86 },
                widthMm: 54,
                heightMm: 86,
                quantity: 1,
                status: 'ready'
            });
        }

        ptState.items = ptState.items.concat(items);
        ptRenderFileList();
        ptUpdateProcessButton();

        // Switch to layout tool tab
        // embed（控制台）：通知父页切到价签二级页；iframe 内侧栏是藏的，
        // 只点 sidebarBtns[0] 会停在价签面板且切不回工牌（2026-09-22）。
        if (window.parent && window.parent !== window) {
            try {
                window.parent.postMessage({ ic: 'tools-goto', tab: 'pricetag' }, location.origin);
            } catch (e) { /* 跨域兜底：仍切本帧 */ sidebarBtns[0].click(); }
        } else {
            sidebarBtns[0].click();
        }
    }

    function bdShowPage(index) {
        if (badgeState.mode === 'badges') {
            var badgeIndex = Math.floor(index / 2);
            var isBack = index % 2 === 1;
            var badge = badgeState.badges[badgeIndex];
            var canvas = isBack ? badge.back : badge.front;
            bd.previewCanvas.width = canvas.width;
            bd.previewCanvas.height = canvas.height;
            bd.previewCanvas.getContext('2d').drawImage(canvas, 0, 0);
            bd.pageInfo.textContent = (index + 1) + ' / ' + badgeState.badges.length * 2;
            bd.prevPage.disabled = index === 0;
            bd.nextPage.disabled = index === badgeState.badges.length * 2 - 1;
        } else {
            var page = badgeState.a4Pages[index];
            bd.previewCanvas.width = page.width;
            bd.previewCanvas.height = page.height;
            bd.previewCanvas.getContext('2d').drawImage(page, 0, 0);
            bd.pageInfo.textContent = (index + 1) + ' / ' + badgeState.a4Pages.length;
            bd.prevPage.disabled = index === 0;
            bd.nextPage.disabled = index === badgeState.a4Pages.length - 1;
        }
    }

    function bdDisplayBadges() {
        bdShowPage(0);
        var total = badgeState.badges.length * 2;
        bd.previewPagination.style.display = total > 1 ? 'flex' : 'none';
        bd.resultInfo.innerHTML = '<span>共生成: ' + badgeState.badges.length + '人，正反面共 ' + total + ' 张工牌</span>';
        bd.downloadBtn.textContent = total === 1 ? '下载JPG' : '下载ZIP';
        previewPanel.style.display = '';
        bd.previewSection.style.display = 'block';
    }

    function bdDisplayA4() {
        bdShowPage(0);
        bd.previewPagination.style.display = badgeState.a4Pages.length > 1 ? 'flex' : 'none';
        bd.resultInfo.innerHTML = '<span>共排版: ' + badgeState.a4Pages.length + ' 页A4（正面+背面交替）</span>';
        bd.downloadBtn.textContent = badgeState.a4Pages.length === 1 ? '下载JPG' : '下载ZIP';
        previewPanel.style.display = '';
        bd.previewSection.style.display = 'block';
    }

    async function bdDownload() {
        try {
        var fmt = document.querySelector('input[name="badgeExportFormat"]:checked').value;
        var ext = fmt === 'png' ? 'png' : 'jpg';
        var mime = fmt === 'png' ? 'image/png' : 'image/jpeg';
        var ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);

        if (badgeState.mode === 'badges') {
            var total = badgeState.badges.length * 2;
            if (total === 1) {
                var canvas = badgeState.badges[0].front;
                Exporter.download(canvas.toDataURL(mime, 0.95), 'badge-front-' + ts + '.' + ext);
                return;
            }
            var zip = new JSZip();
            for (var i = 0; i < badgeState.badges.length; i++) {
                var b = badgeState.badges[i];
                var num = String(i + 1).padStart(2, '0');
                var frontBlob = await new Promise(function(resolve) {
                    b.front.toBlob(function(blob) { resolve(blob); }, mime, 0.95);
                });
                zip.file('badge-front-' + num + '.' + ext, frontBlob);
                var backBlob = await new Promise(function(resolve) {
                    b.back.toBlob(function(blob) { resolve(blob); }, mime, 0.95);
                });
                zip.file('badge-back-' + num + '.' + ext, backBlob);
            }
            var zipBlob = await zip.generateAsync({ type: 'blob' });
            Exporter.downloadBlob(zipBlob, 'badges-' + ts + '.zip');
        } else {
            if (badgeState.a4Pages.length === 1) {
                try {
                    var blob1 = await new Promise(function(resolve, reject) {
                        badgeState.a4Pages[0].toBlob(function(b) {
                            if (b) resolve(b);
                            else reject(new Error('toBlob返回null'));
                        }, mime, 0.95);
                    });
                    Exporter.downloadBlob(blob1, 'badge-a4-' + ts + '.' + ext);
                } catch (e) {
                    Exporter.download(badgeState.a4Pages[0].toDataURL(mime, 0.95), 'badge-a4-' + ts + '.' + ext);
                }
                return;
            }
            var zip2 = new JSZip();
            for (var j = 0; j < badgeState.a4Pages.length; j++) {
                var pageBlob = await new Promise(function(resolve) {
                    badgeState.a4Pages[j].toBlob(function(blob) { resolve(blob); }, mime, 0.95);
                });
                var label = j % 2 === 0 ? 'front' : 'back';
                var num2 = String(Math.floor(j / 2) + 1).padStart(2, '0');
                zip2.file('badge-a4-' + label + '-' + num2 + '.' + ext, pageBlob);
            }
            var zipBlob2 = await zip2.generateAsync({ type: 'blob' });
            Exporter.downloadBlob(zipBlob2, 'badges-a4-' + ts + '.zip');
        }
        } catch (e) {
            console.error('下载失败:', e);
            alert('下载失败: ' + e.message);
        }
    }

    function bdClearAll() {
        badgeState.persons = [];
        badgeState.badges = [];
        badgeState.a4Pages = [];
        badgeState.currentPage = 0;
        badgeState.mode = 'badges';
        bdAddPerson();
        bd.previewSection.style.display = 'none';
        bd.previewPagination.style.display = 'none';
        previewPanel.style.display = 'none';
    }

    // ===== Utilities =====
    function escapeHtml(value) {
        return value.replace(/[&<>"]/g, function(char) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[char];
        });
    }

    // ===== Init =====
    document.addEventListener('DOMContentLoaded', function() {
        ptInit();
        BadgeGenerator.init(function() {
            bdInit();
        });
    });
})();
