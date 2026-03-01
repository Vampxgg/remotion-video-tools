import argparse
import subprocess
import shlex
import json
import sys
import shutil
from pathlib import Path

# --- 配置 ---
DEFAULT_TARGET_DURATION_SECONDS = 5 * 60  # 默认目标时长：5分钟


def get_video_duration(video_path: Path) -> float:
    """使用 ffprobe 获取视频时长（秒）"""
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        print("错误: ffprobe 未找到。请确保 FFmpeg 已安装并已添加到系统环境变量中。", file=sys.stderr)
        sys.exit(1)

    command = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        print(f"错误: ffprobe 执行失败。无法获取视频 '{video_path}' 的时长。", file=sys.stderr)
        print(f"ffprobe 错误信息: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, IndexError):
        print(f"错误: 无法从 ffprobe 的输出中解析时长。", file=sys.stderr)
        sys.exit(1)


def generate_audio_filter(speed_factor: float) -> str:
    """
    为 FFmpeg 生成链式的 atempo 音频过滤器。
    atempo 过滤器接受 0.5 到 100.0 之间的值，但为了稳定，通常建议单次不超过 2.0 或 4.0。
    这里使用 2.0 作为基准，通过链式调用实现任意倍速。
    """
    if speed_factor <= 0.5:
        return "atempo=0.5"  # 限制最小速度

    filters = []
    # 通过多次应用 atempo=2.0 来达到高倍速
    temp_factor = speed_factor
    while temp_factor > 2.0:
        filters.append("atempo=2.0")
        temp_factor /= 2.0

    # 添加剩余的倍速部分
    if temp_factor > 0.5:  # 确保因子在有效范围内
        filters.append(f"atempo={temp_factor:.4f}")

    return ",".join(filters)


def process_video(input_path: Path, output_path: Path, target_duration: int):
    """处理视频，将其调整到目标时长内"""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        print("错误: ffmpeg 未找到。请确保 FFmpeg 已安装并已添加到系统环境变量中。", file=sys.stderr)
        sys.exit(1)

    if not input_path.exists():
        print(f"错误: 输入文件不存在: '{input_path}'", file=sys.stderr)
        sys.exit(1)

    print(f"正在分析视频: {input_path.name}")
    original_duration = get_video_duration(input_path)
    print(f"原始时长: {original_duration:.2f} 秒 ({original_duration / 60:.2f} 分钟)")

    if original_duration <= target_duration:
        print(f"视频时长已在 {target_duration} 秒内，无需处理。")
        # 如果需要，可以将源文件复制到目标位置
        # print(f"正在将文件复制到 '{output_path}'...")
        # shutil.copy(input_path, output_path)
        return

    # 1. 计算所需的速度倍率
    speed_factor = original_duration / target_duration
    print(f"目标时长: {target_duration} 秒")
    print(f"计算加速倍率: {speed_factor:.2f}x")

    # 2. 构建 FFmpeg 命令
    # -vf "setpts=PTS/{speed_factor}" 加速视频
    # -af "{audio_filter}" 加速音频
    # -an 如果没有音频流，则忽略音频处理错误
    video_filter = f"setpts=PTS/{speed_factor:.4f}"
    audio_filter = generate_audio_filter(speed_factor)

    # 使用 -crf 参数来平衡质量和文件大小，18 是高质量，23 是默认，28 是较低质量
    # -preset veryfast 可以加快编码速度，牺牲一点压缩率
    command = [
        ffmpeg_path,
        "-i", str(input_path),
        "-vf", video_filter,
        "-af", audio_filter,
        "-preset", "veryfast",
        "-crf", "22",
        "-an",  # 如果源视频没有音轨，可忽略 "-af" 过滤器可能产生的错误
        str(output_path)
    ]

    # 重新构建命令，移除 -an 并处理可能的无音频情况
    # 这是一个更健壮的做法：先检查是否有音频流
    # (为了简化主流程，这里采用简单的 -an，但专业场景下会先用 ffprobe 探测)

    # 最终命令，处理无音轨视频
    final_command = [
        ffmpeg_path,
        "-i", str(input_path),
        "-vf", video_filter,
    ]
    # 仅当需要加速音频时才添加音频过滤器
    if speed_factor > 0:
        final_command.extend(["-af", audio_filter])

    final_command.extend([
        "-preset", "veryfast",
        "-crf", "22",
        str(output_path)
    ])

    print("\n" + "=" * 50)
    print("将要执行以下 FFmpeg 命令:")
    # 使用 shlex.join 以安全的方式打印命令（适用于 Python 3.8+）
    if sys.version_info >= (3, 8):
        print(shlex.join(final_command))
    else:
        print(" ".join(map(shlex.quote, final_command)))
    print("=" * 50 + "\n")

    # 3. 执行命令
    try:
        print("开始处理，这可能需要一些时间...")
        subprocess.run(final_command, check=True, capture_output=True, text=True, encoding='utf-8')
        print("\n处理完成！")

        final_duration = get_video_duration(output_path)
        print(f"输出文件: '{output_path}'")
        print(f"最终时长: {final_duration:.2f} 秒 ({final_duration / 60:.2f} 分钟)")

    except subprocess.CalledProcessError as e:
        print("错误: FFmpeg 处理失败。", file=sys.stderr)
        print(f"FFmpeg 返回码: {e.returncode}", file=sys.stderr)
        print(f"FFmpeg 标准输出:\n{e.stdout}", file=sys.stderr)
        print(f"FFmpeg 错误输出:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="一个专业的视频时长调整脚本，可将视频加速至指定时长（默认为5分钟）以内。",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "input",
        type=str,
        help="输入视频文件的路径。"
    )
    parser.add_argument(
        "output",
        type=str,
        help="输出视频文件的路径。"
    )
    parser.add_argument(
        "-t", "--target-duration",
        type=int,
        default=DEFAULT_TARGET_DURATION_SECONDS,
        help=f"目标时长（秒）。默认为 {DEFAULT_TARGET_DURATION_SECONDS} 秒 (5分钟)。"
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    process_video(input_path, output_path, args.target_duration)


if __name__ == "__main__":
    main()
