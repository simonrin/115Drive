using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

class Installer : Form {
 readonly Button install=new Button{Text="安装并打开",Width=150,Height=36};
 readonly Button browse=new Button{Text="浏览…",Width=80,Height=30};
 readonly TextBox folder=new TextBox{Width=390};
 readonly Label status=new Label{AutoSize=false,Width=480,Height=66};
 readonly ProgressBar progress=new ProgressBar{Width=480,Height=10};
 bool busy;
 Installer() {
  Text="安装或升级 115Drive";ClientSize=new Size(536,354);StartPosition=FormStartPosition.CenterScreen;FormBorderStyle=FormBorderStyle.FixedDialog;MaximizeBox=false;Font=new Font("Microsoft YaHei UI",10);BackColor=ColorTranslator.FromHtml("#F7F8FA");ForeColor=ColorTranslator.FromHtml("#243247");
  Icon=TwoDrive.CloudBrand.CreateIcon("app",32);
  var mark=new PictureBox{Image=TwoDrive.CloudBrand.Render(128,"app"),Location=new Point(28,22),Size=new Size(54,54),SizeMode=PictureBoxSizeMode.Zoom};
  FormClosed+=(s,e)=>{mark.Image.Dispose();Icon.Dispose();};FormClosing+=(s,e)=>{if(busy)e.Cancel=true;};
  install.FlatStyle=FlatStyle.Flat;install.FlatAppearance.BorderSize=0;install.BackColor=ColorTranslator.FromHtml(TwoDrive.CloudBrand.Indigo);install.ForeColor=Color.White;
  var title=new Label{Text="115Drive",ForeColor=ColorTranslator.FromHtml(TwoDrive.CloudBrand.Indigo),Font=new Font("Microsoft YaHei UI",23,FontStyle.Bold),AutoSize=true,Location=new Point(96,27)};
  var description=new Label{Text="安装到原程序目录即可升级。\n保留账号、同步配置、文件、历史和原有暂停状态。",AutoSize=true,Location=new Point(30,85)};
  folder.Location=new Point(30,145);folder.Text=DefaultFolder();browse.Location=new Point(430,143);
  browse.Click+=(s,e)=>{using(var dialog=new FolderBrowserDialog{Description="选择程序目录（不是同步文件夹）",SelectedPath=folder.Text})if(dialog.ShowDialog()==DialogResult.OK)folder.Text=dialog.SelectedPath;};
  folder.TextChanged+=(s,e)=>UpdateText();status.Location=new Point(30,190);progress.Location=new Point(30,277);install.Location=new Point(355,303);
  install.Click+=async (s,e)=>{
   busy=true;install.Enabled=browse.Enabled=folder.Enabled=false;progress.Style=ProgressBarStyle.Marquee;string target=folder.Text;
   try {status.Text="正在校验安装包并等待同步停止…\n升级时请保持此窗口打开。";string warning=await Task.Run(()=>Install(target));status.Text="升级完成，正在打开设置…";Launch(target);if(warning!="")MessageBox.Show(warning,"115Drive",MessageBoxButtons.OK,MessageBoxIcon.Information);busy=false;Close();}
   catch(Exception error){status.Text=error.Message;busy=false;install.Enabled=browse.Enabled=folder.Enabled=true;progress.Style=ProgressBarStyle.Blocks;}
  };
  Controls.AddRange(new Control[]{mark,title,description,folder,browse,status,progress,install});UpdateText();
 }
 void UpdateText() {
  bool existing=File.Exists(Path.Combine(folder.Text,"115Drive.exe"));install.Text=existing?"升级并打开":"安装并打开";
  status.Text=existing?"已检测到旧程序，将在原目录升级。\n仅替换程序文件；保留用户数据并备份旧程序。":"首次安装请选择空目录。\n已有安装或便携版请选择原程序目录，无需重新扫码。";
 }
 static string DefaultFolder() {
  try {
   var request=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:5245/desktop");request.Proxy=null;request.Timeout=900;
   using(var response=request.GetResponse())using(var reader=new StreamReader(response.GetResponseStream(),Encoding.UTF8)) {
    var value=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(reader.ReadToEnd());string path=Convert.ToString(value["root"]);
    if(File.Exists(Path.Combine(path,"115Drive.exe"))&&File.Exists(Path.Combine(path,"service/TwoDrive.Service.exe")))return path;
   }
  }catch{}
  return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"115Drive");
 }
 static void CheckPath(string path) {
  path=Path.GetFullPath(path);
  foreach(var current in AncestorsAndSelf(path))if((Directory.Exists(current)||File.Exists(current))&&(File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("安装路径不能包含目录联接或符号链接。");
 }
 static IEnumerable<string> AncestorsAndSelf(string path) {for(var current=new DirectoryInfo(path);current!=null;current=current.Parent)yield return current.FullName;}
 static void Extract(string target) {
  target=Path.GetFullPath(target);CheckPath(target);
  if(Directory.Exists(target)&&Directory.GetFileSystemEntries(target).Length!=0)throw new InvalidOperationException("解包测试只能使用空目录。");
  Directory.CreateDirectory(target);
  using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))using(var archive=new ZipArchive(payload,ZipArchiveMode.Read)) {
   var seen=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
   foreach(var entry in archive.Entries) {
    string name=entry.FullName.Replace('\\','/');
    if(name.StartsWith("/")||name.Contains(":")||Array.Exists(name.TrimEnd('/').Split('/'),p=>p=="."||p==".."||p==""||p.EndsWith(" ")||p.EndsWith(".")))throw new InvalidDataException("Invalid package path");
    string path=Path.GetFullPath(Path.Combine(target,name));
    if(!path.StartsWith(target+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase)||!seen.Add(path))throw new InvalidDataException("Invalid package path");CheckPath(path);
    if(name.EndsWith("/")){Directory.CreateDirectory(path);continue;}Directory.CreateDirectory(Path.GetDirectoryName(path));
    using(var input=entry.Open())using(var output=new FileStream(path,FileMode.CreateNew))input.CopyTo(output);
   }
  }
 }
 static void RemoveStage(string path) {
  CheckPath(path);
  foreach(string entry in Directory.GetFileSystemEntries(path)) {
   CheckPath(entry);
   if(Directory.Exists(entry))RemoveStage(entry);else File.Delete(entry);
  }
  Directory.Delete(path);
 }
 static string Install(string target) {
  target=Path.GetFullPath(target);CheckPath(target);
  if(target==Path.GetPathRoot(target)||target.StartsWith("\\\\"))throw new InvalidOperationException("请选择本地磁盘中的程序文件夹。");
  target=target.TrimEnd('\\','/');
  string stage=Path.Combine(Path.GetTempPath(),"115Drive-setup-"+Guid.NewGuid().ToString("N")),warning="";
  try {
   Extract(stage);
   var info=new ProcessStartInfo(Path.Combine(stage,"service/TwoDrive.Service.exe"),"upgrade --root \""+target+"\" --payload \""+stage+"\""){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden,WorkingDirectory=stage};
   using(var worker=Process.Start(info)) {
    worker.WaitForExit();string report=Path.Combine(stage,"upgrade-result.json");
    var result=File.Exists(report)?new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(report,Encoding.UTF8)):null;
    if(worker.ExitCode!=0||result==null||!Object.Equals(result["ok"],true))throw new InvalidOperationException(result!=null&&result.ContainsKey("error")?Convert.ToString(result["error"]):"安装未完成。请重新运行安装包恢复；原配置和同步历史不会重置。");
    if(result.ContainsKey("restart_warning"))warning=Convert.ToString(result["restart_warning"]);
   }
  }finally {
   // Only this unique installer-owned directory; never recursively delete the destination.
   try {if(Directory.Exists(stage))RemoveStage(stage);}catch{}
  }
  return warning;
 }
 static void Launch(string target) {Process.Start(new ProcessStartInfo(Path.Combine(Path.GetFullPath(target),"115Drive.exe"),"--settings"){UseShellExecute=true,WorkingDirectory=target});}
 [STAThread] static int Main(string[] args) {
  try {
   if(args.Length==2&&args[0]=="--extract-only"){Extract(args[1]);return 0;}
   if(args.Length==2&&args[0]=="--install-to"){Install(args[1]);return 0;}
   Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new Installer());return 0;
  }catch(Exception error){
   if(args.Length==2) {try{string path=Path.GetFullPath(args[1]);if(path!=Path.GetPathRoot(path))File.WriteAllText(path.TrimEnd('\\','/')+".error.txt",error.Message);}catch{}}
   else MessageBox.Show(error.Message,"115Drive");return 1;
  }
 }
}
