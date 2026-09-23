/**
 * 导出模块
 * 负责将Canvas导出为JPG并提供下载
 */

const Exporter = {
    /**
     * 将Canvas导出为Blob
     * @param {HTMLCanvasElement} canvas
     * @param {number} quality - 质量 (0-1)
     * @returns {Promise<Blob>}
     */
    async canvasToBlob(canvas, quality = 0.95) {
        return new Promise((resolve) => {
            canvas.toBlob((blob) => {
                resolve(blob);
            }, 'image/jpeg', quality);
        });
    },

    /**
     * 下载图片
     * @param {string} dataURL - 图片数据URL
     * @param {string} filename - 文件名
     */
    download(dataURL, filename = 'price-tag-collage.jpg') {
        const link = document.createElement('a');
        link.href = dataURL;
        link.download = filename;

        // 触发下载
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    },

    /**
     * 下载Canvas内容
     * @param {HTMLCanvasElement} canvas
     * @param {string} filename - 文件名
     */
    async downloadCanvas(canvas, filename = 'price-tag-collage.jpg') {
        const dataURL = canvas.toDataURL('image/jpeg', 0.95);
        this.download(dataURL, filename);
    },

    /**
     * 下载Blob
     * @param {Blob} blob
     * @param {string} filename
     */
    downloadBlob(blob, filename = 'price-tag-collage.jpg') {
        const url = URL.createObjectURL(blob);
        this.download(url, filename);
        // 延迟释放URL
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = Exporter;
}
