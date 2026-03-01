from yt_dlp import YoutubeDL

ydl_opts = {
    'outtmpl': '%(id)s.%(ext)s',
    'format': 'best',

    # 🔥 关键：直接读取浏览器 cookies
    'cookiesfrombrowser': ('chrome',),

    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'referer': 'https://www.douyin.com/',
}

with YoutubeDL(ydl_opts) as ydl:
    ydl.download([
        'https://www.douyin.com/video/7588890507753852202'
    ])
