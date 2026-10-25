using System;
using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace UFOS.ai.ModuleTiles
{
    /// <summary>
    /// The privacy blur of the "Last view" picture (TILES_TASK: stored ONLY blurred — numbers unreadable, layout recognisable;
    /// the blur happens in memory BEFORE anything is written, an unblurred image never touches disk).
    ///
    /// Recipe <see cref="TileLastViewStore.BlurRecipe"/> = "area-1/10+box5x5x2":
    ///  1. exact area average down to 1/10 of the width and height (every output pixel = mean of a 10×10 source block);
    ///  2. a 5×5 box blur, twice, on the small image (≈ a 9-px Gaussian at the small scale = ≈ 90 px of the window).
    /// An 8–10 pt number (11–13 px glyphs at 96 DPI, more on a scaled display — the factor is applied to physical pixels, so the
    /// result is even coarser there) ends up ≈ 1 px tall and spread over ≈ 9 px: unreadable. Panels, colours and chart shapes
    /// stay recognisable. Managed code only (deterministic, no GPU scaler whose quality could vary).
    ///
    /// <see cref="BlurredPng"/> can only be created here, and <see cref="TileLastViewStore"/> accepts only that type — so the
    /// one write path of the capture takes the blurred bytes by construction.
    /// </summary>
    public static class TileBlur
    {
        public const int Factor = 10;
        private const int BoxRadius = 2;   // 5×5
        private const int BoxPasses = 2;

        /// <summary>PNG bytes of a blurred picture (only <see cref="BlurToPng"/> creates it).</summary>
        public sealed class BlurredPng
        {
            internal BlurredPng(byte[] bytes, int width, int height)
            {
                Bytes = bytes;
                Width = width;
                Height = height;
            }

            internal byte[] Bytes { get; }
            public int Width { get; }
            public int Height { get; }
        }

        /// <summary>Blurs <paramref name="source"/> (kept in memory by the caller and dropped right after) and encodes it as PNG.</summary>
        public static BlurredPng BlurToPng(BitmapSource source)
        {
            if (source == null) throw new ArgumentNullException(nameof(source));
            var bgra = source.Format == PixelFormats.Bgra32
                ? source
                : new FormatConvertedBitmap(source, PixelFormats.Bgra32, null, 0);
            int w = bgra.PixelWidth, h = bgra.PixelHeight;
            if (w < Factor || h < Factor) throw new ArgumentException("picture too small to blur (" + w + "×" + h + ")");
            var stride = w * 4;
            var full = new byte[stride * h];
            bgra.CopyPixels(full, stride, 0);

            int sw = w / Factor, sh = h / Factor;
            var small = AreaDownscale(full, w, h, sw, sh);
            Array.Clear(full);   // the unblurred pixels are not kept around longer than needed

            for (var i = 0; i < BoxPasses; i++) small = BoxBlur(small, sw, sh, BoxRadius);

            var bmp = BitmapSource.Create(sw, sh, 96, 96, PixelFormats.Bgra32, null, ToBytes(small), sw * 4);
            var enc = new PngBitmapEncoder();
            enc.Frames.Add(BitmapFrame.Create(bmp));
            using var ms = new MemoryStream();
            enc.Save(ms);
            return new BlurredPng(ms.ToArray(), sw, sh);
        }

        /// <summary>Exact block mean per channel (straight BGRA; the window captures are opaque apart from rounded corners).</summary>
        private static float[] AreaDownscale(byte[] src, int w, int h, int sw, int sh)
        {
            var dst = new float[sw * sh * 4];
            const float inv = 1f / (Factor * Factor);
            for (var y = 0; y < sh; y++)
            {
                for (var x = 0; x < sw; x++)
                {
                    float b = 0, g = 0, r = 0, a = 0;
                    for (var yy = 0; yy < Factor; yy++)
                    {
                        var row = ((y * Factor + yy) * w + x * Factor) * 4;
                        for (var xx = 0; xx < Factor; xx++)
                        {
                            var i = row + xx * 4;
                            b += src[i];
                            g += src[i + 1];
                            r += src[i + 2];
                            a += src[i + 3];
                        }
                    }
                    var o = (y * sw + x) * 4;
                    dst[o] = b * inv;
                    dst[o + 1] = g * inv;
                    dst[o + 2] = r * inv;
                    dst[o + 3] = a * inv;
                }
            }
            return dst;
        }

        /// <summary>Separable box blur with clamped edges.</summary>
        private static float[] BoxBlur(float[] src, int w, int h, int radius)
        {
            var tmp = new float[src.Length];
            var dst = new float[src.Length];
            var n = 2 * radius + 1;
            for (var y = 0; y < h; y++)
                for (var x = 0; x < w; x++)
                    for (var c = 0; c < 4; c++)
                    {
                        float sum = 0;
                        for (var k = -radius; k <= radius; k++)
                        {
                            var xx = Math.Clamp(x + k, 0, w - 1);
                            sum += src[(y * w + xx) * 4 + c];
                        }
                        tmp[(y * w + x) * 4 + c] = sum / n;
                    }
            for (var y = 0; y < h; y++)
                for (var x = 0; x < w; x++)
                    for (var c = 0; c < 4; c++)
                    {
                        float sum = 0;
                        for (var k = -radius; k <= radius; k++)
                        {
                            var yy = Math.Clamp(y + k, 0, h - 1);
                            sum += tmp[(yy * w + x) * 4 + c];
                        }
                        dst[(y * w + x) * 4 + c] = sum / n;
                    }
            return dst;
        }

        private static byte[] ToBytes(float[] px)
        {
            var b = new byte[px.Length];
            for (var i = 0; i < px.Length; i++) b[i] = (byte)Math.Clamp((int)MathF.Round(px[i]), 0, 255);
            return b;
        }
    }
}
