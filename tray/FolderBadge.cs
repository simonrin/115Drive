using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;

public static class FolderBadgeAssets {
    [StructLayout(LayoutKind.Sequential)] struct Size { public int X,Y; public Size(int n){X=n;Y=n;} }
    [ComImport,Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IImageFactory { [PreserveSig] int GetImage(Size size,uint flags,out IntPtr bitmap); }
    [DllImport("shell32.dll",CharSet=CharSet.Unicode,PreserveSig=true)]
    static extern int SHCreateItemFromParsingName(string path,IntPtr context,ref Guid iid,out IImageFactory item);
    [StructLayout(LayoutKind.Sequential)] struct NativeBitmap { public int Type,Width,Height,WidthBytes;public ushort Planes,BitsPixel;public IntPtr Bits; }
    [StructLayout(LayoutKind.Sequential)] struct BitmapInfo {
        public uint Size; public int Width,Height; public ushort Planes,BitCount;
        public uint Compression,SizeImage;public int XPelsPerMeter,YPelsPerMeter;public uint ClrUsed,ClrImportant,Colors;
    }
    [DllImport("gdi32.dll")] static extern int GetObject(IntPtr bitmap,int size,out NativeBitmap result);
    [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")] static extern int GetDIBits(IntPtr dc,IntPtr bitmap,uint start,uint lines,byte[] bits,ref BitmapInfo info,uint usage);
    [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr handle);

    static Bitmap CopyBitmap(IntPtr handle) {
        NativeBitmap native;
        if(GetObject(handle,Marshal.SizeOf(typeof(NativeBitmap)),out native)==0)throw new InvalidOperationException("GetObject failed.");
        var info=new BitmapInfo{Size=40,Width=native.Width,Height=-native.Height,Planes=1,BitCount=32};
        var bytes=new byte[native.Width*native.Height*4];
        var dc=CreateCompatibleDC(IntPtr.Zero);
        try { if(GetDIBits(dc,handle,0,(uint)native.Height,bytes,ref info,0)!=native.Height)throw new InvalidOperationException("GetDIBits failed."); }
        finally { DeleteDC(dc); }
        bool hasAlpha=false;for(int i=3;i<bytes.Length;i+=4)hasAlpha|=bytes[i]!=0;
        if(!hasAlpha)for(int i=3;i<bytes.Length;i+=4)bytes[i]=255;
        var bitmap=new Bitmap(native.Width,native.Height,PixelFormat.Format32bppPArgb);
        var data=bitmap.LockBits(new Rectangle(0,0,bitmap.Width,bitmap.Height),ImageLockMode.WriteOnly,bitmap.PixelFormat);
        try { Marshal.Copy(bytes,0,data.Scan0,bytes.Length); }finally{bitmap.UnlockBits(data);}
        return bitmap;
    }
    public static void Build(string folder,string output) {
        var iid=typeof(IImageFactory).GUID;IImageFactory item;
        Marshal.ThrowExceptionForHR(SHCreateItemFromParsingName(folder,IntPtr.Zero,ref iid,out item));
        int[] sizes={16,20,24,32,40,48,64,128,256};
        var images=new List<byte[]>();
        try {
            foreach(int size in sizes) {
                IntPtr handle;uint flags=0x11; // Cached thumbnails/icons only; do not read cloud file contents.
                int hr=item.GetImage(new Size(size),flags,out handle);
                if(hr<0){flags=0x5;hr=item.GetImage(new Size(size),flags,out handle);} // Icon-only fallback.
                Marshal.ThrowExceptionForHR(hr);
                Bitmap source;
                try { source=CopyBitmap(handle); } finally { DeleteObject(handle); }
                using(var original=source) {
                    using(var bitmap=new Bitmap(size,size,PixelFormat.Format32bppArgb)) {
                        using(var g=Graphics.FromImage(bitmap)) {
                            g.SmoothingMode=SmoothingMode.AntiAlias;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
                            g.InterpolationMode=InterpolationMode.HighQualityBicubic;
                            float scale=Math.Min(size/(float)original.Width,size/(float)original.Height);
                            float w=original.Width*scale,h=original.Height*scale;
                            g.DrawImage(original,new RectangleF((size-w)/2,(size-h)/2,w,h));
                            using(var mark=TwoDrive.CloudBrand.Render(128,"app"))
                                g.DrawImage(mark,new RectangleF(size*.56f,size*.58f,size*.44f,size*.44f));
                        }
                        using(var png=new MemoryStream()){bitmap.Save(png,ImageFormat.Png);images.Add(png.ToArray());}
                    }
                }
            }
        } finally {Marshal.FinalReleaseComObject(item);}
        Directory.CreateDirectory(Path.GetDirectoryName(output));
        using(var stream=File.Create(output))using(var writer=new BinaryWriter(stream)) {
            writer.Write((ushort)0);writer.Write((ushort)1);writer.Write((ushort)sizes.Length);
            int offset=6+16*sizes.Length;
            for(int i=0;i<sizes.Length;i++) {
                writer.Write((byte)(sizes[i]==256?0:sizes[i]));writer.Write((byte)(sizes[i]==256?0:sizes[i]));
                writer.Write((ushort)0);writer.Write((ushort)1);writer.Write((ushort)32);
                writer.Write(images[i].Length);writer.Write(offset);offset+=images[i].Length;
            }
            foreach(var bytes in images)writer.Write(bytes);
        }
    }

    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct RootInfo {
        public long FileId;public uint Hydration,Population,InSync,Hardlink,Status;
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=256)] public string Name;
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=256)] public string Version;
        public uint IdentityLength;
        [MarshalAs(UnmanagedType.ByValArray,SizeConst=4096)] public byte[] Identity;
    }
    [DllImport("cldapi.dll",CharSet=CharSet.Unicode)] static extern int CfGetSyncRootInfoByPath(string path,uint kind,out RootInfo info,uint size,IntPtr returned);
    [StructLayout(LayoutKind.Sequential)] struct FileInfo {
        public uint Attributes,CreationLow,CreationHigh,AccessLow,AccessHigh,WriteLow,WriteHigh,Volume,SizeHigh,SizeLow,Links,IndexHigh,IndexLow;
    }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateFile(string name,uint access,uint sharing,IntPtr security,uint mode,uint flags,IntPtr template);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(IntPtr handle,out FileInfo info);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    public static string RootIdentity(string path) {
        var handle=CreateFile(path,0,7,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
        if(handle==new IntPtr(-1))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        try {
            FileInfo info;
            if(!GetFileInformationByHandle(handle,out info))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            if((info.Attributes&16)==0)throw new InvalidOperationException("Expected a folder.");
            return info.Volume.ToString("X8")+":"+info.IndexHigh.ToString("X8")+info.IndexLow.ToString("X8");
        } finally {CloseHandle(handle);}
    }
    public static string VerifyRoot(string path,string context) {
        RootInfo info;
        Marshal.ThrowExceptionForHR(CfGetSyncRootInfoByPath(path,1,out info,(uint)Marshal.SizeOf(typeof(RootInfo)),IntPtr.Zero));
        if(info.IdentityLength==0||info.IdentityLength>4096)throw new InvalidOperationException("Missing sync-root identity.");
        var identity=new byte[info.IdentityLength];Buffer.BlockCopy(info.Identity,0,identity,0,identity.Length);
        if(Convert.ToBase64String(identity)!=context)throw new InvalidOperationException("Sync-root ownership mismatch.");
        return RootIdentity(path);
    }
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Settings {
        public uint Size,Mask;public IntPtr ViewId,WebTemplate;public uint WebLength;public IntPtr WebVersion,InfoTip;
        public uint InfoLength;public IntPtr ClassId;public uint Flags;
        [MarshalAs(UnmanagedType.LPWStr)] public string IconFile;
        public uint IconLength;public int IconIndex;public IntPtr Logo;public uint LogoLength;
    }
    [DllImport("shell32.dll",CharSet=CharSet.Unicode,ExactSpelling=true)] static extern int SHGetSetFolderCustomSettings(ref Settings settings,string path,uint mode);
    [DllImport("shell32.dll",CharSet=CharSet.Unicode)] static extern void SHChangeNotify(uint e,uint f,string a,IntPtr b);
    public static void Apply(string path,string icon) {
        var settings=new Settings{Size=(uint)Marshal.SizeOf(typeof(Settings)),Mask=0x10,IconFile=icon,IconIndex=0};
        Marshal.ThrowExceptionForHR(SHGetSetFolderCustomSettings(ref settings,path,2));
        Notify(path);
    }
    public static void Notify(string path) {
        SHChangeNotify(0x2000,0x1005,path,IntPtr.Zero);
        SHChangeNotify(0x1000,0x1005,Path.GetDirectoryName(path),IntPtr.Zero);
    }
}
