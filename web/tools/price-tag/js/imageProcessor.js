/**
 * 图片处理模块
 * 负责图片拼接、描边和布局计算
 */

const ImageProcessor = {
    A4_WIDTH: 210,
    A4_HEIGHT: 297,
    SMALL_TAG_WIDTH: 75,
    SMALL_TAG_HEIGHT: 125,
    LARGE_TAG_WIDTH: 150,
    LARGE_TAG_HEIGHT: 125,
    GAP: 0.2,
    BORDER_WIDTH: 5,
    BORDER_COLOR: '#888888',
    BACKGROUND_COLOR: '#FFFFFF',
    DPI: 600,

    mmToPx: function(mm) {
        return Math.round(mm * this.DPI / 25.4);
    },

    getTagPixelSize: function(size) {
        if (size === 'small') {
            return { width: this.mmToPx(this.SMALL_TAG_WIDTH), height: this.mmToPx(this.SMALL_TAG_HEIGHT) };
        }
        return { width: this.mmToPx(this.LARGE_TAG_WIDTH), height: this.mmToPx(this.LARGE_TAG_HEIGHT) };
    },

    createA4Canvas: function() {
        var canvas = document.createElement('canvas');
        canvas.width = this.mmToPx(this.A4_WIDTH);
        canvas.height = this.mmToPx(this.A4_HEIGHT);
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = this.BACKGROUND_COLOR;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        return canvas;
    },

    scaleToTagCanvas: function(image, size) {
        var tagSize = this.getTagPixelSize(size);
        var canvas = document.createElement('canvas');
        canvas.width = tagSize.width;
        canvas.height = tagSize.height;
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.fillStyle = this.BACKGROUND_COLOR;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
        return canvas;
    },

    calculateLayout: function(sizes, gapX, gapY) {
        if (sizes.length !== 4) { throw new Error('需要4张图片'); }
        var gapXPx = this.mmToPx(gapX);
        var gapYPx = this.mmToPx(gapY);
        var canvasWidth = this.mmToPx(this.A4_WIDTH);
        var canvasHeight = this.mmToPx(this.A4_HEIGHT);
        var totalWidth = sizes[0].width + sizes[1].width + gapXPx;
        var totalHeight = sizes[0].height + sizes[2].height + gapYPx;
        var startX = (canvasWidth - totalWidth) / 2;
        var startY = (canvasHeight - totalHeight) / 2;
        var offsetX = startX;
        var offsetY = startY;
        return [
            { x: offsetX, y: offsetY },
            { x: offsetX + sizes[0].width + gapXPx, y: offsetY },
            { x: offsetX, y: offsetY + sizes[0].height + gapYPx },
            { x: offsetX + sizes[0].width + gapXPx, y: offsetY + sizes[0].height + gapYPx }
        ];
    },

    calculateLargeLayout: function(sizes, position, gapX, gapY) {
        var gapXPx = this.mmToPx(gapX);
        var gapYPx = this.mmToPx(gapY);
        var canvasWidth = this.mmToPx(this.A4_WIDTH);
        var canvasHeight = this.mmToPx(this.A4_HEIGHT);
        var largeWidth = sizes[0].width;
        var largeHeight = sizes[0].height;
        var smallWidth = sizes[2].width;
        var smallHeight = sizes[2].height;
        var smallPairWidth = smallWidth * 2 + gapXPx;
        var largeX = (canvasWidth - largeWidth) / 2;
        var smallX = (canvasWidth - smallPairWidth) / 2;

        if (position === 'both') {
            var bothOffsetY = (canvasHeight - (largeHeight * 2 + gapYPx)) / 2;
            return [
                { x: largeX, y: bothOffsetY },
                { x: largeX, y: bothOffsetY },
                { x: largeX, y: bothOffsetY + largeHeight + gapYPx },
                { x: largeX, y: bothOffsetY + largeHeight + gapYPx }
            ];
        }

        var offsetY = (canvasHeight - (largeHeight + smallHeight + gapYPx)) / 2;

        if (position === 'top') {
            return [
                { x: largeX, y: offsetY },
                { x: largeX, y: offsetY },
                { x: smallX, y: offsetY + largeHeight + gapYPx },
                { x: smallX + smallWidth + gapXPx, y: offsetY + largeHeight + gapYPx }
            ];
        }
        return [
            { x: smallX, y: offsetY },
            { x: smallX + smallWidth + gapXPx, y: offsetY },
            { x: largeX, y: offsetY + smallHeight + gapYPx },
            { x: largeX, y: offsetY + smallHeight + gapYPx }
        ];
    },

    collage: function(images, mode, largePosition, gapX, gapY, lineStyle, scale) {
        mode = mode || 'small';
        largePosition = largePosition || 'top';
        gapX = gapX !== undefined ? gapX : 0.2;
        gapY = gapY !== undefined ? gapY : 0.2;
        scale = scale || 1;
        if (images.length !== 4) { throw new Error('需要4张图片'); }
        var canvas = this.createA4Canvas();
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        var self = this;
        var slotSizes = this.getSlotSizes(mode, largePosition);
        var tagImages = images.map(function(img, index) { return img ? self.scaleToTagCanvas(img, slotSizes[index]) : null; });
        var scaledSizes = tagImages.map(function(img) { return img ? { width: img.width * scale, height: img.height * scale } : { width: 0, height: 0 }; });
        var positions = mode === 'small' ? this.calculateLayout(scaledSizes, gapX, gapY) : this.calculateLargeLayout(scaledSizes, largePosition, gapX, gapY);
        for (var i = 0; i < 4; i++) {
            if (!tagImages[i]) continue;
            if (mode === 'large' && (i === 1 || (largePosition === 'both' && i === 3))) continue;
            ctx.drawImage(tagImages[i], 0, 0, tagImages[i].width, tagImages[i].height,
                positions[i].x, positions[i].y, scaledSizes[i].width, scaledSizes[i].height);
        }
        this.drawDashedCutLines(ctx, positions, scaledSizes, mode, largePosition, lineStyle);
        return canvas;
    },

    getSlotSizes: function(mode, largePosition) {
        if (mode === 'small') {
            return ['small', 'small', 'small', 'small'];
        }
        if (largePosition === 'top') {
            return ['large', 'large', 'small', 'small'];
        }
        if (largePosition === 'bottom') {
            return ['small', 'small', 'large', 'large'];
        }
        return ['large', 'large', 'large', 'large'];
    },

    drawDashedCutLines: function(ctx, positions, sizes, mode, largePosition, lineStyle) {
        var canvasWidth = this.mmToPx(this.A4_WIDTH);
        var canvasHeight = this.mmToPx(this.A4_HEIGHT);
        var activeIndices = [];
        for (var i = 0; i < 4; i++) {
            if (mode === 'large' && (i === 1 || (largePosition === 'both' && i === 3))) continue;
            activeIndices.push(i);
        }

        // 检查线段是否穿过某个标签区域
        function segBlockedH(y, x1, x2, excludeIdx) {
            for (var j = 0; j < activeIndices.length; j++) {
                if (j === excludeIdx) continue;
                var op = positions[activeIndices[j]], os = sizes[activeIndices[j]];
                if (y > op.y && y < op.y + os.height && x1 < op.x + os.width && x2 > op.x) return true;
            }
            return false;
        }
        function segBlockedV(x, y1, y2, excludeIdx) {
            for (var j = 0; j < activeIndices.length; j++) {
                if (j === excludeIdx) continue;
                var op = positions[activeIndices[j]], os = sizes[activeIndices[j]];
                if (x > op.x && x < op.x + os.width && y1 < op.y + os.height && y2 > op.y) return true;
            }
            return false;
        }

        ctx.save();
        ctx.strokeStyle = this.BORDER_COLOR;
        ctx.lineWidth = this.BORDER_WIDTH;
        ctx.setLineDash(lineStyle === 'dashed' ? [10, 8] : []);

        for (var k = 0; k < activeIndices.length; k++) {
            var idx = activeIndices[k];
            var p = positions[idx];
            var s = sizes[idx];
            var topY = p.y, bottomY = p.y + s.height;
            var leftX = p.x, rightX = p.x + s.width;

            // 顶部水平线
            if (!segBlockedH(topY, leftX, rightX, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, topY); ctx.lineTo(rightX, topY); ctx.stroke();
            }
            // 顶部竖线向上延伸
            if (!segBlockedV(leftX, 0, topY, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, 0); ctx.lineTo(leftX, topY); ctx.stroke();
            }
            if (!segBlockedV(rightX, 0, topY, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, 0); ctx.lineTo(rightX, topY); ctx.stroke();
            }

            // 底部水平线
            if (!segBlockedH(bottomY, leftX, rightX, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, bottomY); ctx.lineTo(rightX, bottomY); ctx.stroke();
            }
            // 底部竖线向下延伸
            if (!segBlockedV(leftX, bottomY, canvasHeight, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, bottomY); ctx.lineTo(leftX, canvasHeight); ctx.stroke();
            }
            if (!segBlockedV(rightX, bottomY, canvasHeight, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, bottomY); ctx.lineTo(rightX, canvasHeight); ctx.stroke();
            }

            // 左侧竖线
            if (!segBlockedV(leftX, topY, bottomY, k)) {
                ctx.beginPath(); ctx.moveTo(leftX, topY); ctx.lineTo(leftX, bottomY); ctx.stroke();
            }
            // 左侧横线向左延伸
            if (!segBlockedH(topY, 0, leftX, k)) {
                ctx.beginPath(); ctx.moveTo(0, topY); ctx.lineTo(leftX, topY); ctx.stroke();
            }
            if (!segBlockedH(bottomY, 0, leftX, k)) {
                ctx.beginPath(); ctx.moveTo(0, bottomY); ctx.lineTo(leftX, bottomY); ctx.stroke();
            }

            // 右侧竖线
            if (!segBlockedV(rightX, topY, bottomY, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, topY); ctx.lineTo(rightX, bottomY); ctx.stroke();
            }
            // 右侧横线向右延伸
            if (!segBlockedH(topY, rightX, canvasWidth, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, topY); ctx.lineTo(canvasWidth, topY); ctx.stroke();
            }
            if (!segBlockedH(bottomY, rightX, canvasWidth, k)) {
                ctx.beginPath(); ctx.moveTo(rightX, bottomY); ctx.lineTo(canvasWidth, bottomY); ctx.stroke();
            }
        }

        ctx.restore();
    },

    canvasToDataURL: function(canvas, quality) {
        quality = quality || 0.95;
        return canvas.toDataURL('image/jpeg', quality);
    },

    detectSizeFromPixels: function(pixelWidth, pixelHeight) {
        var aspect = pixelWidth / pixelHeight;

        if (Math.abs(aspect - 54 / 86) < 0.05) {
            return { size: 'badge', widthMm: 54, heightMm: 86 };
        }
        if (Math.abs(aspect - 75 / 125) < 0.05) {
            return { size: 'small', widthMm: 75, heightMm: 125 };
        }
        if (Math.abs(aspect - 150 / 125) < 0.05) {
            return { size: 'large', widthMm: 150, heightMm: 125 };
        }

        var widthMm = 75;
        var heightMm = Math.round(widthMm / aspect);
        return { size: 'custom_' + widthMm + '_' + heightMm, widthMm: widthMm, heightMm: heightMm };
    },

    getSizeMm: function(sizeInfo) {
        if (sizeInfo && sizeInfo.widthMm) {
            return { width: sizeInfo.widthMm, height: sizeInfo.heightMm };
        }
        var s = typeof sizeInfo === 'string' ? sizeInfo : 'small';
        switch (s) {
            case 'small': return { width: 75, height: 125 };
            case 'large': return { width: 150, height: 125 };
            case 'badge': return { width: 54, height: 86 };
            default: return { width: 75, height: 125 };
        }
    }
};

if (typeof module !== 'undefined' && module.exports) { module.exports = ImageProcessor; }
