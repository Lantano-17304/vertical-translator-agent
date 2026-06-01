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