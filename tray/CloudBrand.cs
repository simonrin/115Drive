using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

namespace TwoDrive {
 // Vector cloud silhouette, with optical simplification for notification-area sizes.
 public static class CloudBrand {
  public const string Indigo="#375F83",Paper="#FFFFFF";
  static Color Hex(string value) { return ColorTranslator.FromHtml(value); }
  static GraphicsPath Cloud(bool compact) {
   var p=new GraphicsPath();
   p.AddBezier(12,49,1,49,-1,32,9,28);
   p.AddBezier(9,28,5,16,15,11,23,16);
   p.AddBezier(23,16,26,2,48,3,48,24);
   if(compact) {
    p.AddBezier(48,24,61,22,65,40,54,46);
    p.AddBezier(54,46,47,52,42,52,35,48);
   } else {
    p.AddBezier(48,24,57,22,52,38,62,31);
    p.AddBezier(62,31,61,42,53,44,48,43);
    p.AddBezier(48,43,46,53,39,55,33,49);
   }
   p.AddBezier(compact?35:33,48,25,58,17,55,12,49);
   p.CloseFigure();return p;
  }
  static Pen Stroke(Color color,float width) {
   return new Pen(color,width){StartCap=LineCap.Round,EndCap=LineCap.Round,LineJoin=LineJoin.Round};
  }
  public static Bitmap Render(int size,string mode) {
   if(size<16||size>1024)throw new ArgumentOutOfRangeException("size");
   var bitmap=new Bitmap(size,size,PixelFormat.Format32bppArgb);
   using(var g=Graphics.FromImage(bitmap)) {
    g.SmoothingMode=SmoothingMode.AntiAlias;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
    g.ScaleTransform(size/64f,size/64f);
    if(mode=="keep-local") {
     using(var fill=new SolidBrush(Hex("#247A55")))g.FillEllipse(fill,5,5,54,54);
     using(var pen=Stroke(Color.White,6))g.DrawLines(pen,new[]{new PointF(17,32),new PointF(28,43),new PointF(47,21)});
     return bitmap;
    }
    if(mode=="free-space") {
     using(var cloud=Cloud(true))using(var pen=Stroke(Hex("#376F9D"),4))g.DrawPath(pen,cloud);
     return bitmap;
    }
    bool compact=size<=32,paused=mode=="paused",error=mode=="error";
    using(var cloud=Cloud(compact))using(var fill=new SolidBrush(Hex("#376F9D")))using(var rim=Stroke(Hex("#ECF3FA"),compact?2.5f:1.5f)) {
     g.FillPath(fill,cloud);g.DrawPath(rim,cloud);
    }
    // One open curl at larger sizes; solid lobes remain legible at 16-32 px.
    if(!compact)using(var curl=new GraphicsPath())using(var pen=Stroke(Color.White,3.5f)) {
     curl.AddBezier(18,38,8,32,16,22,24,27);
     curl.AddBezier(24,27,27,17,39,19,41,30);
     curl.AddBezier(41,30,45,46,25,49,24,37);
     curl.AddBezier(24,37,23,30,32,29,33,35);
     g.DrawPath(pen,curl);
    }
    if(mode!="app") {
     // Seven pixels wide even at 16 px, with shape as well as colour encoding.
     using(var rim=new SolidBrush(Color.White))g.FillEllipse(rim,34,33,30,30);
     using(var badge=new SolidBrush(Hex(error?"#B84141":paused?"#667385":mode=="idle"?"#247A55":"#375F83")))g.FillEllipse(badge,37,36,24,24);
     using(var pen=Stroke(Color.White,compact?4:3.2f)) {
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
