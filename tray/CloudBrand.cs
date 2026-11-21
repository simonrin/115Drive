using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

namespace TwoDrive {
 // Rounded two-tone cloud shared by the app, its tray states and folder badges.
 public static class CloudBrand {
  public const string Indigo="#235C90",Paper="#FFFFFF",MarkRed="#CC555C",MarkBlue="#387EB1";
  static Color Hex(string value) { return ColorTranslator.FromHtml(value); }
  static GraphicsPath Cloud() {
   var p=new GraphicsPath();
   p.AddBezier(17,49,1,49,0,28,15,25);
   p.AddBezier(15,25,18,6,45,6,49,25);
   p.AddBezier(49,25,65,26,65,49,49,49);
   p.AddLine(49,49,17,49);
   p.CloseFigure();return p;
  }
  static Pen Stroke(Color color,float width) {
   return new Pen(color,width){StartCap=LineCap.Round,EndCap=LineCap.Round,LineJoin=LineJoin.Round};
  }
  static void DrawCloudMark(Graphics g,bool compact) {
   using(var outline=new GraphicsPath())
   using(var front=new GraphicsPath()) {
    // The small coral cloud and the blue crown share a continuous outer edge.
    outline.AddBezier(16,52,8,52,2,47,2,39);
    outline.AddBezier(2,39,2,33,5,28,10,26);
    outline.AddBezier(10,26,11,19,18,16,24,20);
    front.AddPath(outline,false);
    outline.AddBezier(24,20,27,12,33,9,40,10);
    outline.AddBezier(40,10,49,11,54,17,54,25);
    outline.AddBezier(54,25,59,27,62,32,62,38);
    outline.AddBezier(62,38,62,46,56,52,48,52);
    outline.CloseFigure();
    front.AddBezier(24,20,22,23,21,27,22,30);
    front.AddBezier(22,30,30,28,36,33,36,40);
    front.AddBezier(36,40,36,47,31,52,24,52);
    front.CloseFigure();
    using(var sky=new LinearGradientBrush(new Point(0,8),new Point(0,54),Hex(compact?MarkBlue:"#62AFD8"),Hex(compact?MarkBlue:"#256699")))
    using(var coral=new LinearGradientBrush(new Point(0,16),new Point(0,54),Hex(compact?MarkRed:"#F09580"),Hex(compact?MarkRed:"#C45162")))
    using(var rim=Stroke(Color.FromArgb(compact?238:210,248,251,255),compact?2.8f:1.25f)) {
     g.FillPath(sky,outline);
     g.FillPath(coral,front);
     g.DrawPath(rim,outline);
    }
   }
  }
  static Bitmap RenderFolder(int size) {
   var bitmap=new Bitmap(size,size,PixelFormat.Format32bppArgb);
   using(var g=Graphics.FromImage(bitmap)) {
    g.SmoothingMode=SmoothingMode.AntiAlias;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
    g.InterpolationMode=InterpolationMode.HighQualityBicubic;
    g.ScaleTransform(size/64f,size/64f);
    // Upright yellow folder with the app cloud in front.
    using(var back=new SolidBrush(Hex("#FFDF7E")))
     g.FillPolygon(back,new[]{new PointF(12,5),new PointF(45,5),new PointF(47,7),new PointF(47,55),new PointF(24,61),new PointF(12,53)});
    using(var inside=new SolidBrush(Hex("#F3CC62")))
     g.FillPolygon(inside,new[]{new PointF(12,5),new PointF(25,16),new PointF(45,16),new PointF(45,5)});
    using(var face=new LinearGradientBrush(new Point(25,16),new Point(47,61),Hex("#FFE59E"),Hex("#F5BE52")))
     g.FillPolygon(face,new[]{new PointF(25,16),new PointF(47,16),new PointF(47,55),new PointF(25,61)});
    using(var edge=new SolidBrush(Hex("#FFF0B4")))
     g.FillPolygon(edge,new[]{new PointF(12,5),new PointF(25,16),new PointF(25,61),new PointF(12,53)});
    using(var mark=Render(Math.Max(16,(int)Math.Round(size*.63)),"app"))g.DrawImage(mark,new RectangleF(23,23,40,40));
   }
   return bitmap;
  }
  public static Bitmap Render(int size,string mode) {
   if(size<16||size>1024)throw new ArgumentOutOfRangeException("size");
   if(mode=="folder")return RenderFolder(size);
   var bitmap=new Bitmap(size,size,PixelFormat.Format32bppArgb);
   using(var g=Graphics.FromImage(bitmap)) {
    g.SmoothingMode=SmoothingMode.AntiAlias;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
    g.ScaleTransform(size/64f,size/64f);
    if(mode=="keep-local") {
     using(var rim=new SolidBrush(Hex("#D6EDE3")))g.FillEllipse(rim,3,3,58,58);
     using(var fill=new SolidBrush(Hex("#197553")))g.FillEllipse(fill,5,5,54,54);
     using(var pen=Stroke(Color.White,6))g.DrawLines(pen,new[]{new PointF(17,32),new PointF(28,43),new PointF(47,21)});
     return bitmap;
    }
    if(mode=="free-space") {
     using(var cloud=Cloud())
     using(var rim=Stroke(Hex("#D5E8F6"),size<=24?10:7))
     using(var pen=Stroke(Hex(Indigo),size<=24?5.5f:4)) {
      g.DrawPath(rim,cloud);
      g.DrawPath(pen,cloud);
     }
     return bitmap;
    }
    bool compact=size<=32,paused=mode=="paused",error=mode=="error";
    var placement=g.Save();
    if(mode!="app")g.TranslateTransform(0,-4);
    DrawCloudMark(g,compact);
    g.Restore(placement);
    if(mode!="app") {
     // Seven pixels wide even at 16 px, with shape as well as colour encoding.
     using(var rim=new SolidBrush(Color.White))g.FillEllipse(rim,32,31,32,32);
     using(var badge=new SolidBrush(Hex(error?"#AC3842":paused?"#52627A":mode=="idle"?"#197553":Indigo)))g.FillEllipse(badge,35,34,26,26);
     using(var pen=Stroke(Color.White,compact?4.5f:3.6f)) {
      if(mode=="idle")g.DrawLines(pen,new[]{new PointF(42,48),new PointF(47,53),new PointF(55,43)});
      else if(paused){g.DrawLine(pen,45,42,45,54);g.DrawLine(pen,53,42,53,54);}
      else if(error){g.DrawLine(pen,49,41,49,48);g.DrawLine(pen,49,54,49,54.2f);}
      else {g.DrawArc(pen,42,41,14,14,45,280);g.DrawLines(pen,new[]{new PointF(51,40),new PointF(56,43),new PointF(52,47)});}
     }
    }
   }
   return bitmap;
  }
  public static byte[] IconBytes(string mode) {
   int[] sizes={16,20,24,32,40,48,64,128,256};var images=new List<byte[]>();
   foreach(int size in sizes)using(var bitmap=Render(size,mode))using(var png=new MemoryStream()) {
    bitmap.Save(png,ImageFormat.Png);images.Add(png.ToArray());
   }
   using(var output=new MemoryStream())using(var writer=new BinaryWriter(output)) {
    writer.Write((ushort)0);writer.Write((ushort)1);writer.Write((ushort)sizes.Length);
    int offset=6+16*sizes.Length;
    for(int i=0;i<sizes.Length;i++) {
     writer.Write((byte)(sizes[i]==256?0:sizes[i]));writer.Write((byte)(sizes[i]==256?0:sizes[i]));
     writer.Write((byte)0);writer.Write((byte)0);writer.Write((ushort)1);writer.Write((ushort)32);
     writer.Write(images[i].Length);writer.Write(offset);offset+=images[i].Length;
    }
    foreach(var bytes in images)writer.Write(bytes);writer.Flush();return output.ToArray();
   }
  }
  public static Icon CreateIcon(string mode,int size) {
   using(var input=new MemoryStream(IconBytes(mode)))using(var icon=new Icon(input,size,size))return (Icon)icon.Clone();
  }
  public static void WriteIcon(string path) { File.WriteAllBytes(path,IconBytes("app")); }
 }
}
