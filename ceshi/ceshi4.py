import yt_dlp
import json


def download_video_with_yt_dlp(url: str, cookies_file: str, output_path: str = "."):
    """
    使用 yt-dlp 库和 cookies 文件下载视频。

    Args:
        url (str): 视频的 URL.
        cookies_file (str): 存储 cookies 的文本文件路径.
        output_path (str): 视频保存的目录，默认为当前目录.
    """
    # yt-dlp 的配置选项
    ydl_opts = {
        # ******** 核心改动在这里 ********
        'cookiefile': cookies_file,  # 指定 cookies 文件路径
        # *******************************

        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': f'{output_path}/%(title)s.%(ext)s',
        'noplaylist': True,
        'quiet': False,
        'progress': True,
        'merge_output_format': 'mp4',  # 确保最终合并为 mp4
    }

    print(f"[*] 准备使用 yt-dlp 下载: {url}")
    print(f"[*] 使用 Cookies 文件: {cookies_file}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            error_code = ydl.download([url])
            if error_code == 0:
                print("\n[+] 视频下载成功！")
            else:
                print(f"\n[!] 视频下载失败，错误码: {error_code}")

    except yt_dlp.utils.DownloadError as e:
        print(f"\n[!] 下载出错: {e}")
    except Exception as e:
        print(f"\n[!] 发生未知错误: {e}")


if __name__ == "__main__":
    video_url = "https://www.douyin.com/video/7587734466659912998"

    # 确保这个文件名和你创建的 cookies 文件名一致
    cookie_filename = "./cookie.txt"

    download_video_with_yt_dlp(video_url, cookie_filename)

