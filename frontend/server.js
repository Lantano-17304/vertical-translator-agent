const express = require('express');
const cors = require('cors');
const path = require('path');
const http = require('http');
const { createProxyMiddleware } = require('http-proxy-middleware');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// BFF层：把后端 Python 的 SSE 流式数据原样转发给前端
function proxySSE(backendUrl, res) {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');

    http.get(backendUrl, (backendRes) => {
        backendRes.on('data', (chunk) => res.write(chunk));
        backendRes.on('end', () => res.end());
    }).on('error', (e) => {
        console.error(`Problem with request: ${e.message}`);
        res.write(`data: {"error": "Backend offline"}\n\n`);
        res.end();
    });
}

// 下载：把后端二进制下载响应原样转发给前端
function proxyDownload(backendUrl, res) {
    http.get(backendUrl, (backendRes) => {
        res.statusCode = backendRes.statusCode || 502;
        // 透传常用下载头
        const headersToPass = [
            'content-type',
            'content-disposition',
            'content-length',
            'cache-control',
        ];
        headersToPass.forEach((h) => {
            const v = backendRes.headers[h];
            if (v) res.setHeader(h, v);
        });
        backendRes.pipe(res);
    }).on('error', (e) => {
        console.error(`Problem with request: ${e.message}`);
        res.status(502).json({ error: 'Backend offline', detail: e.message });
    });
}

function buildBackendQuery(path, query) {
    const params = new URLSearchParams();
    Object.entries(query || {}).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') {
            params.set(key, value);
        }
    });
    const qs = params.toString();
    return `http://127.0.0.1:8000${path}${qs ? `?${qs}` : ''}`;
}

// 获取 YouTube 视频可选画质
app.get('/api/youtube-video-formats', (req, res) => {
    const url = req.query.url;
    if (!url) {
        return res.status(400).json({ error: 'url query parameter is required' });
    }
    const backendUrl = buildBackendQuery('/youtube_video_formats', { url });
    http.get(backendUrl, (backendRes) => {
        let body = '';
        backendRes.on('data', (chunk) => { body += chunk; });
        backendRes.on('end', () => {
            res.statusCode = backendRes.statusCode || 502;
            res.setHeader('Content-Type', backendRes.headers['content-type'] || 'application/json');
            res.end(body);
        });
    }).on('error', (e) => {
        res.status(502).json({ error: 'Backend offline', detail: e.message });
    });
});

// 流式下载 YouTube 原视频（含进度）
app.get('/api/stream-download-youtube-video', (req, res) => {
    const { url, quality } = req.query;
    if (!url) {
        return res.status(400).json({ error: 'url query parameter is required' });
    }
    const backendUrl = buildBackendQuery('/stream_download_youtube_video', { url, quality: quality || 'best' });
    proxySSE(backendUrl, res);
});

// 按 token 拉取已下载的视频文件
app.get('/api/download-youtube-video-file', (req, res) => {
    const token = req.query.token;
    if (!token) {
        return res.status(400).json({ error: 'token query parameter is required' });
    }
    const backendUrl = buildBackendQuery('/download_youtube_video_file', { token });
    proxyDownload(backendUrl, res);
});

// 下载 YouTube 原视频（直连，无进度；保留给脚本使用）
app.get('/api/download-youtube-video', (req, res) => {
    const { url, quality } = req.query;
    if (!url) {
        return res.status(400).json({ error: 'url query parameter is required' });
    }
    const backendUrl = buildBackendQuery('/download_youtube_video', { url, quality: quality || 'best' });
    proxyDownload(backendUrl, res);
});

// 单句翻译
app.get('/api/translate', (req, res) => {
    const text = req.query.text;
    if (!text) {
        return res.status(400).json({ error: 'Text query parameter is required' });
    }
    const backendUrl = `http://127.0.0.1:8000/stream_translate?text=${encodeURIComponent(text)}`;
    proxySSE(backendUrl, res);
});

// YouTube 链接翻译（页面流式预览）
app.get('/api/translate-youtube', (req, res) => {
    const url = req.query.url;
    if (!url) {
        return res.status(400).json({ error: 'url query parameter is required' });
    }
    const backendUrl = `http://127.0.0.1:8000/stream_translate_youtube?url=${encodeURIComponent(url)}`;
    proxySSE(backendUrl, res);
});

// YouTube 链接 -> 流式生成并下载翻译 SRT（含进度与 Agent 思考过程）
app.get('/api/translate-youtube-srt', (req, res) => {
    const url = req.query.url;
    if (!url) {
        return res.status(400).json({ error: 'url query parameter is required' });
    }
    const backendUrl = `http://127.0.0.1:8000/stream_translate_youtube_srt?url=${encodeURIComponent(url)}`;
    proxySSE(backendUrl, res);
});

// 处理文件上传的代理 (自动处理 multipart/form-data)
app.post('/api/translate-srt', createProxyMiddleware({
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
}));

app.listen(PORT, () => {
    console.log(`BFF Server running at http://localhost:${PORT}`);
});