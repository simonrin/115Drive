using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading.Tasks;
using System.Windows.Forms;

class Installer : Form {
 readonly Button install=new Button{Text="安装并打开",Width=130,Height=36};
 readonly Label status=new Label{AutoSize=false,Width=480,Height=55};
 readonly ProgressBar progress=new ProgressBar{Width=480,Height=10};
 readonly string destination=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"115Drive");
 Installer() {
  Text="安装 115Drive";ClientSize=new Size(536,300);StartPosition=FormStartPosition.CenterScreen;FormBorderStyle=FormBorderStyle.FixedDialog;MaximizeBox=false;Font=new Font("Microsoft YaHei UI",10);BackColor=Color.White;
  var title=new Label{Text="115Drive",Font=new Font("Microsoft YaHei UI",23,FontStyle.Bold),AutoSize=true,Location=new Point(28,25)};
  var description=new Label{Text="115 网盘双向同步 · 原生托盘与设置\n自带运行组件，无需另外安装 Python。",AutoSize=true,Location=new Point(30,85)};
  status.Location=new Point(30,145);status.Text="安装到当前用户目录：\n"+destination;
  progress.Location=new Point(30,213);install.Location=new Point(375,244);
  install.Click+=async (s,e)=>{install.Enabled=false;progress.Style=ProgressBarStyle.Marquee;try{await Task.Run(()=>Extract(destination));status.Text="安装完成，正在打开设置…";Process.Start(new ProcessStartInfo(Path.Combine(destination,"115Drive.exe"),"--settings"){UseShellExecute=true});Close();}catch(Exception error){status.Text=error.Message;install.Enabled=true;progress.Style=ProgressBarStyle.Blocks;}};
  Controls.AddRange(new Control[]{title,description,status,progress,install});
 }
 static void Extract(string target) {
  target=Path.GetFullPath(target);Directory.CreateDirectory(target);
  // Never replace an existing app or its private state implicitly.
  if(Directory.GetFileSystemEntries(target).Length!=0)throw new InvalidOperationException("目标目录已有文件。此安装包仅用于全新安装；现有用户请保留数据并使用便携包更新程序文件。");
  if((File.GetAttributes(target)&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("安装目录不能是目录联接。");
  foreach(var ancestor in Ancestors(target))if((File.GetAttributes(ancestor)&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("安装路径包含目录联接。");
  using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
  using(var archive=new ZipArchive(payload,ZipArchiveMode.Read)) {
   foreach(var entry in archive.Entries) {
    string path=Path.GetFullPath(Path.Combine(target,entry.FullName));
    if(!path.StartsWith(target+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("Invalid package path");
    if(entry.FullName.EndsWith("/")){Directory.CreateDirectory(path);continue;}
    Directory.CreateDirectory(Path.GetDirectoryName(path));
    using(var input=entry.Open())using(var output=new FileStream(path,FileMode.CreateNew))input.CopyTo(output);
   }
  }
 }
 static System.Collections.Generic.IEnumerable<string> Ancestors(string path) {for(var parent=Directory.GetParent(path);parent!=null;parent=parent.Parent)yield return parent.FullName;}
 [STAThread] static int Main(string[] args) {
  try {
   // Reproducible installer QA: extracts only to an explicitly supplied EMPTY directory.
   if(args.Length==2&&args[0]=="--extract-only"){Extract(args[1]);return 0;}
   Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new Installer());return 0;
  }catch(Exception error){if(args.Length==2)File.WriteAllText(args[1]+".error.txt",error.Message);else MessageBox.Show(error.Message,"115Drive");return 1;}
 }
}
