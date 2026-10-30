using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

namespace TwoDrive {
 // Shared curve-based mark for the tray, windows, installer and multi-size ICO.
 public static class CloudBrand {
  static Color Hex(string value) { return ColorTranslator.FromHtml(value); }
  static GraphicsPath Cloud() {
   var p=new GraphicsPath();
   // One unbroken line: unwind the main scroll, sweep the low cloud contour,
   // then finish in the smaller return. No branches, internal lines or tail.
   p.AddBezier(31,30,34,30,35,27,32,25);p.AddBezier(32,25,28,22,24,25,25,29);
   p.AddBezier(25,29,26,36,35,36,39,32);p.AddBezier(39,32,43,28,41,20,35,18);
   p.AddBezier(35,18,28,15,22,19,21,24);p.AddBezier(21,24,16,21,10,24,11,29);
   p.AddBezier(11,29,6,30,5,35,8,39);p.AddBezier(8,39,13,45,22,41,30,41);
   p.AddBezier(30,41,39,40,47,44,55,39);p.AddBezier(55,39,61,35,58,28,53,28);
   p.AddBezier(53,28,49,27,46,30,48,33);p.AddBezier(48,33,49,36,53,35,53,32);
   return p;
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
    bool paused=mode=="paused",error=mode=="error";
    // Clear blue on a transparent background, with no tile or gradient.
    Color ink=Hex(paused?"#8B96A6":"#176BEB");
    // Slightly heavier small-size strokes survive the Windows notification area.
    float width=size<=16?5.4f:size<=20?5f:size<=32?4.7f:4.5f;
    using(var path=Cloud())using(var pen=Stroke(ink,width)) {
     if(mode!="app") {
      // Optical sizing for the notification area: use the icon slot rather
      // than the padded app canvas. Keep the stroke round and unscaled.
      RectangleF bounds;
      using(var outline=(GraphicsPath)path.Clone()) {
       outline.Flatten(null,.01f);bounds=outline.GetBounds();
      }
      float sx=(60-width)/bounds.Width,sy=(40-width)/bounds.Height;
      using(var fit=new Matrix(sx,0,0,sy,2+width/2-bounds.X*sx,10+width/2-bounds.Y*sy))path.Transform(fit);
     }
     g.DrawPath(pen,path);
    }
    if(mode!="app") {
     var badgeState=g.Save();
     g.TranslateTransform(6,6);g.ScaleTransform(.9f,.9f);
     using(var rim=new SolidBrush(Hex("#FCFDFE")))g.FillEllipse(rim,42,42,21,21);
     using(var badge=new SolidBrush(Hex(paused?"#97703D":error?"#A83F36":mode=="idle"?"#2D815C":"#176BEB")))g.FillEllipse(badge,44,44,17,17);
     using(var pen=Stroke(Color.White,2.2f)) {
      if(mode=="idle")g.DrawLines(pen,new[]{new PointF(48,52),new PointF(51,55),new PointF(57,49)});
      else if(paused){g.DrawLine(pen,50,49,50,56);g.DrawLine(pen,55,49,55,56);}
      else if(error){g.DrawLine(pen,52.5f,48,52.5f,53);g.DrawLine(pen,52.5f,56,52.5f,56.2f);}
      else {g.DrawArc(pen,48,48,9,9,30,280);g.DrawLines(pen,new[]{new PointF(55,47),new PointF(57,50),new PointF(53,50)});}
     }
     g.Restore(badgeState);
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
  public static Icon CreateIcon(string mode) {
   using(var input=new MemoryStream(IconBytes(mode)))using(var icon=new Icon(input,32,32))return (Icon)icon.Clone();
  }
  public static void WriteIcon(string path) { File.WriteAllBytes(path,IconBytes("app")); }
 }
}
