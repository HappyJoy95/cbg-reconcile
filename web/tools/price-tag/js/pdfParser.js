/**
 * PDF解析模块
 * 负责加载PDF文件并渲染为图片
 */

const PDFParser = {
    loadPDF: function(file) {
        var self = this;
        return new Promise(function(resolve, reject) {
            var reader = new FileReader();
            reader.onload = function(e) {
                var data = new Uint8Array(e.target.result);
                pdfjsLib.getDocument({ data: data }).promise.then(function(pdf) {
                    pdf.getPage(1).then(function(page) {
                        var viewport = page.getViewport({ scale: 1 });
                        resolve({
                            pdf: pdf,
                            page: page,
                            width: viewport.width,
                            height: viewport.height
                        });
                    }).catch(reject);
                }).catch(reject);
            };
            reader.onerror = reject;
            reader.readAsArrayBuffer(file);
        });
    },

    renderToCanvas: function(page, scale) {
        scale = scale || 8;
        return new Promise(function(resolve, reject) {
            var viewport = page.getViewport({ scale: scale });
            var canvas = document.createElement('canvas');
            var context = canvas.getContext('2d');

            canvas.width = viewport.width;
            canvas.height = viewport.height;

            page.render({
                canvasContext: context,
                viewport: viewport
            }).promise.then(function() {
                resolve(canvas);
            }).catch(reject);
        });
    },

    convertToImage: function(file, scale) {
        var self = this;
        scale = scale || 8;
        return new Promise(function(resolve, reject) {
            self.loadPDF(file).then(function(result) {
                var page = result.page;
                var width = result.width;
                var height = result.height;

                self.renderToCanvas(page, scale).then(function(canvas) {
                    resolve({
                        canvas: canvas,
                        width: canvas.width,
                        height: canvas.height,
                        size: self.detectSize(width, height)
                    });
                }).catch(reject);
            }).catch(reject);
        });
    },

    detectSize: function(width, height) {
        var widthCm = width * 0.0352778;
        var heightCm = height * 0.0352778;
        var longSide = Math.max(widthCm, heightCm);
        var shortSide = Math.min(widthCm, heightCm);

        if (Math.abs(shortSide - 7.5) < 0.8 && Math.abs(longSide - 12.5) < 0.8) {
            return { size: 'small', widthMm: 75, heightMm: 125 };
        }
        if (Math.abs(shortSide - 12.5) < 0.8 && Math.abs(longSide - 15.0) < 0.8) {
            return { size: 'large', widthMm: 150, heightMm: 125 };
        }
        return { size: 'unknown', widthMm: 75, heightMm: Math.round(75 * longSide / shortSide) };
    },

    getSizeLabel: function(sizeInfo) {
        var s = typeof sizeInfo === 'string' ? sizeInfo : (sizeInfo ? sizeInfo.size : 'unknown');
        switch (s) {
            case 'small': return '小价签 (7.5x12.5cm)';
            case 'large': return '大价签 (15.0x12.5cm)';
            case 'badge': return '工牌 (5.4x8.6cm)';
            default: return '未知尺寸';
        }
    }
};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = PDFParser;
}
