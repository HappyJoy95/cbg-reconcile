/**
 * 工牌生成模块
 * 负责工牌画布渲染、A4排版、输入处理
 */

var BadgeGenerator = {
    BADGE_WIDTH: 54,
    BADGE_HEIGHT: 86,
    A4_WIDTH: 210,
    A4_HEIGHT: 297,
    DPI: 600,
    defaultQrHuawei: null,

    init: function(callback) {
        var self = this;
        var dataUrl = 'data:image/png;base64,' + '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';
        self.defaultQrHuaweiSrc = dataUrl;
        var img = new Image();
        img.onload = function() {
            self.defaultQrHuawei = img;
            if (callback) callback();
        };
        img.onerror = function() { if (callback) callback(); };
        img.src = dataUrl;
    },

    POSITIONS: {
        '店长': { en: 'Store Leader', color: '#C0C0C0' },
        '店经理': { en: 'Manager', color: '#C0C0C0' },
        '产品专家': { en: 'Expert', color: '#C5A55A' },
        '体验顾问': { en: 'Specialist', color: '#C0C0C0' },
        '见习': { en: 'Trainee', color: '#C0C0C0' }
    },

    FONT_CN: '"PingFang SC", "Microsoft YaHei", "Noto Sans SC", "Hiragino Sans GB", sans-serif',
    FONT_EN: '"SF Pro Text", "Helvetica Neue", Arial, sans-serif',

    mmToPx: function(mm) {
        return Math.round(mm * this.DPI / 25.4);
    },

    createA4Canvas: function() {
        var canvas = document.createElement('canvas');
        canvas.width = this.mmToPx(this.A4_WIDTH);
        canvas.height = this.mmToPx(this.A4_HEIGHT);
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#FFFFFF';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        return canvas;
    },

    renderBadgeFront: function(name, pinyin, position) {
        var w = this.mmToPx(this.BADGE_WIDTH);
        var h = this.mmToPx(this.BADGE_HEIGHT);
        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';

        // Background
        ctx.fillStyle = '#FFFFFF';
        ctx.fillRect(0, 0, w, h);

        // Color strip near bottom, centered with side margins
        var posInfo = this.POSITIONS[position] || this.POSITIONS['见习'];
        var stripH = this.mmToPx(3);
        var stripMarginBottom = this.mmToPx(10);
        var stripSideMargin = this.mmToPx(8);
        ctx.fillStyle = posInfo.color;
        ctx.fillRect(stripSideMargin, h - stripH - stripMarginBottom, w - stripSideMargin * 2, stripH);

        // Left alignment anchor = strip left edge
        var textLeft = stripSideMargin;

        // Chinese name - spaced characters, left aligned
        var nameChars = name.split('').join(' ');
        var nameFontSize = this.mmToPx(6.5);
        ctx.fillStyle = '#1d1d1f';
        ctx.font = '600 ' + nameFontSize + 'px ' + this.FONT_CN;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        var nameY = h * 0.2;
        ctx.fillText(nameChars, textLeft, nameY);

        // Pinyin
        var pinyinFontSize = this.mmToPx(2.8);
        ctx.fillStyle = '#7a7a7a';
        ctx.font = '400 ' + pinyinFontSize + 'px ' + this.FONT_EN;
        ctx.fillText(pinyin, textLeft, nameY + nameFontSize * 0.9);

        // Position Chinese - lower area
        var posFontSize = this.mmToPx(4.5);
        ctx.fillStyle = '#1d1d1f';
        ctx.font = '600 ' + posFontSize + 'px ' + this.FONT_CN;
        var posY = h - stripH - stripMarginBottom - this.mmToPx(9);
        ctx.fillText(position, textLeft, posY);

        // Position English
        var posEnFontSize = this.mmToPx(2.5);
        ctx.fillStyle = '#7a7a7a';
        ctx.font = '400 ' + posEnFontSize + 'px ' + this.FONT_EN;
        ctx.fillText(posInfo.en, textLeft, posY + posFontSize * 0.9);

        return canvas;
    },

    renderBadgeBack: function(qrWecom, qrHuawei) {
        var w = this.mmToPx(this.BADGE_WIDTH);
        var h = this.mmToPx(this.BADGE_HEIGHT);
        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';

        // Background
        ctx.fillStyle = '#FFFFFF';
        ctx.fillRect(0, 0, w, h);

        var qrSize = this.mmToPx(20);
        var qrLabelH = this.mmToPx(2.0) + this.mmToPx(1.5);
        var centerX = w / 2;
        var huaweiQR = qrHuawei || this.defaultQrHuawei;
        var hasHuawei = !!huaweiQR;
        var hasWecom = !!qrWecom;

        // 计算垂直布局：QR + 标签 为一组，两组居中排列
        var qrBlockH = qrSize + qrLabelH;
        var gapBetween = this.mmToPx(6);
        var totalH = 0;
        if (hasHuawei && hasWecom) {
            totalH = qrBlockH * 2 + gapBetween;
        } else if (hasHuawei || hasWecom) {
            totalH = qrBlockH;
        }
        var startY = (h - totalH) / 2;

        // My Huawei QR
        if (hasHuawei) {
            var qr1X = centerX - qrSize / 2;
            var qr1Y = startY;
            ctx.drawImage(huaweiQR, qr1X, qr1Y, qrSize, qrSize);
            ctx.fillStyle = '#7a7a7a';
            ctx.font = '400 ' + this.mmToPx(2.0) + 'px ' + this.FONT_CN;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText('我的华为', centerX, qr1Y + qrSize + this.mmToPx(1.5));
            startY += qrBlockH + gapBetween;
        }

        // WeChat QR
        if (hasWecom) {
            var qr2X = centerX - qrSize / 2;
            var qr2Y = startY;
            ctx.drawImage(qrWecom, qr2X, qr2Y, qrSize, qrSize);
            ctx.fillStyle = '#7a7a7a';
            ctx.font = '400 ' + this.mmToPx(2.0) + 'px ' + this.FONT_CN;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText('企业微信', centerX, qr2Y + qrSize + this.mmToPx(1.5));
        }

        // Privacy text
        ctx.fillStyle = '#1d1d1f';
        ctx.font = '400 ' + this.mmToPx(2.0) + 'px ' + this.FONT_CN;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'bottom';
        ctx.fillText('添加企业微信视为您同意隐私声明', centerX, h - this.mmToPx(8));
        ctx.fillText('（可访问"我的华为"小程序查阅）', centerX, h - this.mmToPx(5));

        return canvas;
    },

    layoutBadgesOnA4: function(badgeCanvases) {
        var cols = 3, rows = 3;
        var perPage = cols * rows;
        var pages = [];

        for (var p = 0; p < badgeCanvases.length; p += perPage) {
            var pageCanvas = this.createA4Canvas();
            var ctx = pageCanvas.getContext('2d');
            ctx.imageSmoothingEnabled = true;
            ctx.imageSmoothingQuality = 'high';

            var gapX = this.mmToPx(-0.2);
            var gapY = this.mmToPx(-0.2);
            var badgeW = this.mmToPx(this.BADGE_WIDTH);
            var badgeH = this.mmToPx(this.BADGE_HEIGHT);
            var totalW = cols * badgeW + (cols - 1) * gapX;
            var totalH = rows * badgeH + (rows - 1) * gapY;
            var startX = (pageCanvas.width - totalW) / 2;
            var startY = (pageCanvas.height - totalH) / 2;

            var count = Math.min(perPage, badgeCanvases.length - p);
            for (var i = 0; i < count; i++) {
                var col = i % cols;
                var row = Math.floor(i / cols);
                var x = startX + col * (badgeW + gapX);
                var y = startY + row * (badgeH + gapY);
                ctx.drawImage(badgeCanvases[p + i], x, y, badgeW, badgeH);
            }

            this.drawCutLines(ctx, startX, startY, badgeW, badgeH, gapX, gapY, cols, rows, count);
            pages.push(pageCanvas);
        }

        return pages;
    },

    drawCutLines: function(ctx, startX, startY, badgeW, badgeH, gapX, gapY, cols, rows, count) {
        var canvasW = this.mmToPx(this.A4_WIDTH);
        var canvasH = this.mmToPx(this.A4_HEIGHT);

        ctx.save();
        ctx.strokeStyle = '#999999';
        ctx.lineWidth = 2;
        ctx.setLineDash([10, 8]);

        // 水平裁剪线
        var drawnRows = Math.ceil(count / cols);
        for (var r = 0; r <= drawnRows; r++) {
            var y = startY + r * (badgeH + gapY) - (r === 0 ? 0 : gapY);
            if (r === 0) y = startY;
            else if (r === drawnRows) y = startY + (drawnRows - 1) * (badgeH + gapY) + badgeH;
            else y = startY + r * (badgeH + gapY);
            ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvasW, y); ctx.stroke();
        }

        // 垂直裁剪线
        var drawnCols = Math.min(cols, count);
        for (var c = 0; c <= drawnCols; c++) {
            var x;
            if (c === 0) x = startX;
            else if (c === drawnCols) x = startX + (drawnCols - 1) * (badgeW + gapX) + badgeW;
            else x = startX + c * (badgeW + gapX);
            ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvasH); ctx.stroke();
        }

        ctx.restore();
    },

    generate: function(persons) {
        var self = this;
        var frontBadges = [];
        var backBadges = [];

        for (var i = 0; i < persons.length; i++) {
            var p = persons[i];
            frontBadges.push(self.renderBadgeFront(p.name, p.pinyin, p.position));
            backBadges.push(self.renderBadgeBack(p.qrWecom, p.qrHuawei));
        }

        var frontPages = self.layoutBadgesOnA4(frontBadges);
        var backPages = self.layoutBadgesOnA4(backBadges);

        // Interleave: front1, back1, front2, back2, ...
        var result = [];
        for (var j = 0; j < frontPages.length; j++) {
            result.push({ canvas: frontPages[j], label: 'front' });
            result.push({ canvas: backPages[j], label: 'back' });
        }

        return result;
    }
};

if (typeof module !== 'undefined' && module.exports) { module.exports = BadgeGenerator; }
