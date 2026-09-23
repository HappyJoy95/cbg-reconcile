/**
 * 通用布局引擎
 * 支持按尺寸自动排版，裁剪线独立图层
 */

var LayoutEngine = {
    A4_WIDTH_MM: 210,
    A4_HEIGHT_MM: 297,
    DPI: 600,
    BORDER_WIDTH: 5,
    BORDER_COLOR: '#888888',
    BACKGROUND_COLOR: '#FFFFFF',

    mmToPx: function(mm) {
        return Math.round(mm * this.DPI / 25.4);
    },

    fitGrid: function(itemWidthMm, itemHeightMm, gapX, gapY, scale) {
        scale = scale || 1;
        var gapXpx = this.mmToPx(gapX !== undefined ? gapX : 0.2);
        var gapYpx = this.mmToPx(gapY !== undefined ? gapY : 0.2);
        var cols = Math.max(1, Math.floor((this.mmToPx(this.A4_WIDTH_MM) + gapXpx) / (this.mmToPx(itemWidthMm) * scale + gapXpx)));
        var rows = Math.max(1, Math.floor((this.mmToPx(this.A4_HEIGHT_MM) + gapYpx) / (this.mmToPx(itemHeightMm) * scale + gapYpx)));
        return { cols: cols, rows: rows, perPage: cols * rows };
    },

    buildPages: function(items, gapX, gapY, scale) {
        scale = scale || 1;
        gapX = gapX !== undefined ? gapX : 0.2;
        gapY = gapY !== undefined ? gapY : 0.2;

        var largeItems = [];
        var smallItems = [];
        var badgeItems = [];
        var customGroups = {};

        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            if (item.sizeKey === 'large') {
                largeItems.push(item);
            } else if (item.sizeKey === 'small') {
                smallItems.push(item);
            } else if (item.sizeKey === 'badge') {
                badgeItems.push(item);
            } else {
                var key = item.sizeKey || ('custom_' + Math.round(item.widthMm) + '_' + Math.round(item.heightMm));
                if (!customGroups[key]) customGroups[key] = [];
                customGroups[key].push(item);
            }
        }

        var pages = [];

        // Large + small mixed layout (preserves existing behavior)
        var twoRowsFit = this.mmToPx(125) * scale * 2 + this.mmToPx(gapY) <= this.mmToPx(this.A4_HEIGHT_MM);
        while (!twoRowsFit && largeItems.length > 0) {
            pages.push({ mode: 'large', largePosition: 'top', items: [largeItems.shift()], widthMm: 150, heightMm: 125 });
        }
        while (largeItems.length >= 2) {
            pages.push({
                mode: 'large', largePosition: 'both',
                items: [largeItems.shift(), largeItems.shift()],
                widthMm: 150, heightMm: 125
            });
        }
        if (largeItems.length === 1) {
            var large = largeItems.shift();
            var pairedSmalls = [smallItems.shift(), smallItems.shift()].filter(Boolean);
            pages.push({
                mode: 'large', largePosition: 'top',
                items: [large].concat(pairedSmalls),
                widthMm: 150, heightMm: 125
            });
        }

        // Small items with mixed page support
        var smallGrid = this.fitGrid(75, 125, gapX, gapY, scale);
        while (smallItems.length > 0) {
            var pageItems = [];
            for (var s = 0; s < smallGrid.perPage && smallItems.length > 0; s++) {
                pageItems.push(smallItems.shift());
            }
            // If last page is not full, try to add badges
            if (smallItems.length === 0 && pageItems.length < smallGrid.perPage) {
                var mixedItems = pageItems.slice();
                var smallRows = Math.ceil(pageItems.length / smallGrid.cols);
                var gapYpx = this.mmToPx(gapY);
                var smallHeight = smallRows * this.mmToPx(125) * scale + (smallRows - 1) * gapYpx;
                var badgeRows = Math.max(0, Math.floor((this.mmToPx(this.A4_HEIGHT_MM) - smallHeight) / (this.mmToPx(86) * scale + gapYpx)));
                var badgeCols = this.fitGrid(54, 86, gapX, gapY, scale).cols;
                var badgeCapacity = badgeRows * badgeCols;
                while (badgeItems.length > 0 && badgeCapacity-- > 0) {
                    mixedItems.push(badgeItems.shift());
                }

                if (mixedItems.length > pageItems.length) {
                    // Create mixed page
                    pages.push({
                        mode: 'mixed',
                        items: mixedItems,
                        widthMm: 75, heightMm: 125
                    });
                } else {
                    pages.push({
                        mode: 'small', largePosition: 'top',
                        items: pageItems,
                        widthMm: 75, heightMm: 125
                    });
                }
            } else {
                pages.push({
                    mode: 'small', largePosition: 'top',
                    items: pageItems,
                    widthMm: 75, heightMm: 125
                });
            }
        }

        // Remaining badge items
        var badgeGrid = this.fitGrid(54, 86, gapX, gapY, scale);
        while (badgeItems.length > 0) {
            var badgePageItems = [];
            for (var b = 0; b < badgeGrid.perPage && badgeItems.length > 0; b++) {
                badgePageItems.push(badgeItems.shift());
            }
            pages.push({
                mode: 'badge', largePosition: 'top',
                items: badgePageItems,
                widthMm: 54, heightMm: 86
            });
        }

        // Custom size groups
        var customKeys = Object.keys(customGroups);
        for (var c = 0; c < customKeys.length; c++) {
            var cKey = customKeys[c];
            var cItems = customGroups[cKey];
            var cW = cItems[0].widthMm;
            var cH = cItems[0].heightMm;
            var cGrid = this.fitGrid(cW, cH, gapX, gapY, scale);
            while (cItems.length > 0) {
                var cPageItems = [];
                for (var ci = 0; ci < cGrid.perPage && cItems.length > 0; ci++) {
                    cPageItems.push(cItems.shift());
                }
                pages.push({
                    mode: 'custom', largePosition: 'top',
                    items: cPageItems,
                    widthMm: cW, heightMm: cH
                });
            }
        }

        return pages;
    },

    renderPage: function(pageData, gapX, gapY, scale) {
        gapX = gapX !== undefined ? gapX : 0.2;
        gapY = gapY !== undefined ? gapY : 0.2;
        scale = scale || 1;

        var canvasW = this.mmToPx(this.A4_WIDTH_MM);
        var canvasH = this.mmToPx(this.A4_HEIGHT_MM);
        var canvas = document.createElement('canvas');
        canvas.width = canvasW;
        canvas.height = canvasH;
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.fillStyle = this.BACKGROUND_COLOR;
        ctx.fillRect(0, 0, canvasW, canvasH);

        // The artwork and cut lines share the same scaled geometry.
        var layout = this.getPositions(pageData, gapX, gapY, scale);
        for (var i = 0; i < pageData.items.length; i++) {
            var img = pageData.items[i].canvas;
            var p = layout.positions[i];
            var size = layout.sizes[i];
            ctx.drawImage(img, 0, 0, img.width, img.height, p.x, p.y, size.width, size.height);
        }
        return canvas;
    },

    getPositions: function(pageData, gapX, gapY, scale) {
        gapX = gapX !== undefined ? gapX : 0.2;
        gapY = gapY !== undefined ? gapY : 0.2;
        scale = scale || 1;

        var canvasW = this.mmToPx(this.A4_WIDTH_MM);
        var canvasH = this.mmToPx(this.A4_HEIGHT_MM);
        var mode = pageData.mode;
        var items = pageData.items;
        var positions = [];
        var sizes = [];

        if (mode === 'large') {
            return this.getLargePositions(pageData, gapX, gapY, scale);
        }

        if (mode === 'mixed') {
            return this.getMixedPositions(pageData, gapX, gapY, scale);
        }

        var itemW = this.mmToPx(pageData.widthMm) * scale;
        var itemH = this.mmToPx(pageData.heightMm) * scale;
        var gapXpx = this.mmToPx(gapX);
        var gapYpx = this.mmToPx(gapY);
        var grid = this.fitGrid(pageData.widthMm, pageData.heightMm, gapX, gapY, scale);
        var cols = grid.cols;
        var rows = Math.ceil(items.length / cols);
        var totalW = cols * itemW + (cols - 1) * gapXpx;
        var totalH = rows * itemH + (rows - 1) * gapYpx;
        var startX = (canvasW - totalW) / 2;
        var startY = (canvasH - totalH) / 2;

        for (var i = 0; i < items.length; i++) {
            var col = i % cols;
            var row = Math.floor(i / cols);
            var sw = itemW;
            var sh = itemH;
            var x = startX + col * (itemW + gapXpx) + (itemW - sw) / 2;
            var y = startY + row * (itemH + gapYpx) + (itemH - sh) / 2;
            positions.push({ x: x, y: y });
            sizes.push({ width: sw, height: sh });
        }

        return { positions: positions, sizes: sizes };
    },

    getLargePositions: function(pageData, gapX, gapY, scale) {
        var canvasW = this.mmToPx(this.A4_WIDTH_MM);
        var canvasH = this.mmToPx(this.A4_HEIGHT_MM);
        var gapXpx = this.mmToPx(gapX);
        var gapYpx = this.mmToPx(gapY);
        var largeW = this.mmToPx(150) * scale;
        var largeH = this.mmToPx(125) * scale;
        var smallW = this.mmToPx(75) * scale;
        var smallH = this.mmToPx(125) * scale;
        var items = pageData.items;
        var position = pageData.largePosition;
        var positions = [];
        var sizes = [];

        if (position === 'both') {
            var totalH = largeH * 2 + gapYpx;
            var startY = (canvasH - totalH) / 2;
            var largeX = (canvasW - largeW) / 2;
            for (var i = 0; i < items.length; i++) {
                var sw = largeW;
                var sh = largeH;
                positions.push({ x: largeX + (largeW - sw) / 2, y: startY + i * (largeH + gapYpx) + (largeH - sh) / 2 });
                sizes.push({ width: sw, height: sh });
            }
        } else {
            var smallPairW = smallW * 2 + gapXpx;
            var totalH2 = items.length > 1 ? largeH + gapYpx + smallH : largeH;
            var offsetY = (canvasH - totalH2) / 2;
            var largeX2 = (canvasW - largeW) / 2;
            var smallX2 = (canvasW - smallPairW) / 2;

            if (items[0]) {
                var sw0 = largeW;
                var sh0 = largeH;
                positions.push({ x: largeX2 + (largeW - sw0) / 2, y: offsetY + (largeH - sh0) / 2 });
                sizes.push({ width: sw0, height: sh0 });
            }
            for (var j = 1; j < items.length; j++) {
                var swJ = smallW;
                var shJ = smallH;
                positions.push({ x: smallX2 + (j - 1) * (smallW + gapXpx) + (smallW - swJ) / 2, y: offsetY + largeH + gapYpx + (smallH - shJ) / 2 });
                sizes.push({ width: swJ, height: shJ });
            }
        }

        return { positions: positions, sizes: sizes };
    },

    getMixedPositions: function(pageData, gapX, gapY, scale) {
        var canvasW = this.mmToPx(this.A4_WIDTH_MM);
        var canvasH = this.mmToPx(this.A4_HEIGHT_MM);
        var gapXpx = this.mmToPx(gapX);
        var gapYpx = this.mmToPx(gapY);
        var items = pageData.items;
        var positions = [];
        var sizes = [];

        // Separate items by size
        var smallItems = [];
        var badgeItems = [];
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            if (item.sizeKey === 'small' || (item.widthMm >= 70 && item.heightMm >= 120)) {
                smallItems.push(item);
            } else {
                badgeItems.push(item);
            }
        }

        var smallW = this.mmToPx(75) * scale;
        var smallH = this.mmToPx(125) * scale;
        var badgeW = this.mmToPx(54) * scale;
        var badgeH = this.mmToPx(86) * scale;

        // Calculate small layout
        var smallCols = Math.min(smallItems.length, this.fitGrid(75, 125, gapX, gapY, scale).cols);
        var smallRows = smallCols > 0 ? Math.ceil(smallItems.length / smallCols) : 0;
        var smallTotalW = smallCols * smallW + (smallCols - 1) * gapXpx;
        var smallTotalH = smallRows * smallH + (smallRows - 1) * gapYpx;
        var smallStartX = (canvasW - smallTotalW) / 2;
        var smallStartY = (canvasH - smallTotalH) / 2;

        // If there are badges, shift small items up
        if (badgeItems.length > 0) {
            var badgeCols = Math.min(badgeItems.length, this.fitGrid(54, 86, gapX, gapY, scale).cols);
            var badgeRows = Math.ceil(badgeItems.length / badgeCols);
            var badgeTotalH = badgeRows * badgeH + (badgeRows - 1) * gapYpx;
            var totalContentH = smallTotalH + gapYpx + badgeTotalH;
            smallStartY = (canvasH - totalContentH) / 2;
        }

        // Small positions
        for (var s = 0; s < smallItems.length; s++) {
            var sCol = s % smallCols;
            var sRow = Math.floor(s / smallCols);
            var ssw = smallW;
            var ssh = smallH;
            positions.push({
                x: smallStartX + sCol * (smallW + gapXpx) + (smallW - ssw) / 2,
                y: smallStartY + sRow * (smallH + gapYpx) + (smallH - ssh) / 2
            });
            sizes.push({ width: ssw, height: ssh });
        }

        // Badge positions
        if (badgeItems.length > 0) {
            var badgeCols = Math.min(badgeItems.length, this.fitGrid(54, 86, gapX, gapY, scale).cols);
            var badgeTotalW = badgeCols * badgeW + (badgeCols - 1) * gapXpx;
            var badgeStartX = (canvasW - badgeTotalW) / 2;
            var badgeStartY = smallStartY + smallTotalH + gapYpx;

            for (var b = 0; b < badgeItems.length; b++) {
                var bCol = b % badgeCols;
                var bRow = Math.floor(b / badgeCols);
                var bsw = badgeW;
                var bsh = badgeH;
                positions.push({
                    x: badgeStartX + bCol * (badgeW + gapXpx) + (badgeW - bsw) / 2,
                    y: badgeStartY + bRow * (badgeH + gapYpx) + (badgeH - bsh) / 2
                });
                sizes.push({ width: bsw, height: bsh });
            }
        }

        return { positions: positions, sizes: sizes };
    },

    renderCutLines: function(pageData, gapX, gapY, scale, lineStyle) {
        var canvasW = this.mmToPx(this.A4_WIDTH_MM);
        var canvasH = this.mmToPx(this.A4_HEIGHT_MM);
        var canvas = document.createElement('canvas');
        canvas.width = canvasW;
        canvas.height = canvasH;
        var ctx = canvas.getContext('2d');

        var layout = this.getPositions(pageData, gapX, gapY, scale);
        var positions = layout.positions;
        var sizes = layout.sizes;
        if (positions.length === 0) return canvas;

        ctx.save();
        ctx.strokeStyle = this.BORDER_COLOR;
        ctx.lineWidth = this.BORDER_WIDTH;
        ctx.setLineDash(lineStyle === 'dashed' ? [10, 8] : []);

        // Collision check helpers
        function segBlockedH(y, x1, x2, excludeIdx) {
            for (var j = 0; j < positions.length; j++) {
                if (j === excludeIdx) continue;
                var op = positions[j], os = sizes[j];
                if (y > op.y && y < op.y + os.height && x1 < op.x + os.width && x2 > op.x) return true;
            }
            return false;
        }
        function segBlockedV(x, y1, y2, excludeIdx) {
            for (var j = 0; j < positions.length; j++) {
                if (j === excludeIdx) continue;
                var op = positions[j], os = sizes[j];
                if (x > op.x && x < op.x + os.width && y1 < op.y + os.height && y2 > op.y) return true;
            }
            return false;
        }

        for (var k = 0; k < positions.length; k++) {
            var p = positions[k];
            var s = sizes[k];
            var topY = p.y, bottomY = p.y + s.height;
            var leftX = p.x, rightX = p.x + s.width;

            // Top horizontal
            if (!segBlockedH(topY, leftX, rightX, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, topY); ctx.lineTo(rightX, topY); ctx.stroke();
            }
            // Top verticals up
            if (!segBlockedV(leftX, 0, topY, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, 0); ctx.lineTo(leftX, topY); ctx.stroke();
            }
            if (!segBlockedV(rightX, 0, topY, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, 0); ctx.lineTo(rightX, topY); ctx.stroke();
            }

            // Bottom horizontal
            if (!segBlockedH(bottomY, leftX, rightX, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, bottomY); ctx.lineTo(rightX, bottomY); ctx.stroke();
            }
            // Bottom verticals down
            if (!segBlockedV(leftX, bottomY, canvasH, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, bottomY); ctx.lineTo(leftX, canvasH); ctx.stroke();
            }
            if (!segBlockedV(rightX, bottomY, canvasH, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, bottomY); ctx.lineTo(rightX, canvasH); ctx.stroke();
            }

            // Left vertical
            if (!segBlockedV(leftX, topY, bottomY, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, topY); ctx.lineTo(leftX, bottomY); ctx.stroke();
            }
            // Left horizontals left
            if (!segBlockedH(topY, 0, leftX, k)) {
                ctx.beginPath(); ctx.moveTo(0, topY); ctx.lineTo(leftX, topY); ctx.stroke();
            }
            if (!segBlockedH(bottomY, 0, leftX, k)) {
                ctx.beginPath(); ctx.moveTo(0, bottomY); ctx.lineTo(leftX, bottomY); ctx.stroke();
            }

            // Right vertical
            if (!segBlockedV(rightX, topY, bottomY, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, topY); ctx.lineTo(rightX, bottomY); ctx.stroke();
            }
            // Right horizontals right
            if (!segBlockedH(topY, rightX, canvasW, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, topY); ctx.lineTo(canvasW, topY); ctx.stroke();
            }
            if (!segBlockedH(bottomY, rightX, canvasW, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, bottomY); ctx.lineTo(canvasW, bottomY); ctx.stroke();
            }
        }

        ctx.restore();
        return canvas;
    },

    composite: function(contentCanvas, cutLineCanvas) {
        var exportCanvas = document.createElement('canvas');
        exportCanvas.width = contentCanvas.width;
        exportCanvas.height = contentCanvas.height;
        var ctx = exportCanvas.getContext('2d');
        ctx.drawImage(contentCanvas, 0, 0);
        if (cutLineCanvas) {
            ctx.drawImage(cutLineCanvas, 0, 0);
        }
        return exportCanvas;
    }
};

if (typeof module !== 'undefined' && module.exports) { module.exports = LayoutEngine; }
