using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Drawing = System.Drawing;
using Forms = System.Windows.Forms;

namespace TwoDrive {
 sealed partial class TrayApp : IDisposable {
  readonly string root;
  readonly bool test;
  readonly JavaScriptSerializer json = new JavaScriptSerializer();
  readonly Window panel;
  readonly Forms.NotifyIcon notify;
  readonly Dictionary<string,Drawing.Icon> icons = new Dictionary<string,Drawing.Icon>();
  readonly DispatcherTimer timer;
  Dictionary<string,object> state = new Dictionary<string,object>();
  bool exiting, sending, activityDialogOpen;
  bool statusSpinning;
  readonly RotateTransform statusRotation=new RotateTransform();
  ContextMenu activityMenu;
  string activityTestRoot="",lastActivityTarget="";
  DateTime hiddenAt = DateTime.MinValue;
  string lastCommand = "", lastActivities = "";
  public EventWaitHandle ShowSignal;
  RegisteredWaitHandle showWait;

  [DllImport("user32.dll")] static extern bool DestroyIcon(IntPtr handle);
  static object Value(Dictionary<string,object> map,string key) { object value; return map != null && map.TryGetValue(key,out value) ? value : null; }
  static Dictionary<string,object> Map(object item) { return item as Dictionary<string,object> ?? new Dictionary<string,object>(); }
  static string Text(Dictionary<string,object> map,string key,string fallback="") { return Convert.ToString(Value(map,key),CultureInfo.InvariantCulture) ?? fallback; }
  static double Number(Dictionary<string,object> map,string key) { double result; return Double.TryParse(Text(map,key),NumberStyles.Any,CultureInfo.InvariantCulture,out result) && !Double.IsInfinity(result) && !Double.IsNaN(result) ? result : 0; }
  static IEnumerable<Dictionary<string,object>> Rows(object items) { var array = items as object[]; return array == null ? Enumerable.Empty<Dictionary<string,object>>() : array.Select(Map); }
  static Brush Brush(string value) { return (Brush)new BrushConverter().ConvertFromString(value); }
  T Find<T>(string name) where T:class { return panel.FindName(name) as T; }
  TextBlock Label(string name) { return Find<TextBlock>(name); }
  Dictionary<string,object> Read(string path) { return Map(ReadAny(path)); }
  object ReadAny(string path) { try { using(var file=new FileStream(Path.Combine(root,path),FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))using(var reader=new StreamReader(file,Encoding.UTF8))return json.DeserializeObject(reader.ReadToEnd()); } catch { return null; } }
  void Save(string path,object data) { File.WriteAllText(Path.Combine(root,path),json.Serialize(data),new UTF8Encoding(false)); }
  static string Bytes(double value) {
   if(value<1024) return Math.Max(0,value).ToString("0",CultureInfo.InvariantCulture)+" B";
   string[] units={"KiB","MiB","GiB","TiB"}; int unit=0; value/=1024;
   while(value>=1024 && unit<units.Length-1) {value/=1024;unit++;}
   return value.ToString(value<10?"0.00":"0.0",CultureInfo.InvariantCulture)+" "+units[unit];
  }
  static string TimeLabel(string value) {
   DateTimeOffset time; if(!DateTimeOffset.TryParse(value,out time))return "";
   var local=time.LocalDateTime;
   return local.Date==DateTime.Today?local.ToString("HH:mm"):local.ToString("MM/dd HH:mm");
  }
  static string FileLabel(string value) { return value.Replace('\\','/').Split('/').LastOrDefault() ?? value; }
  static bool Retrying(Dictionary<string,object> model) {
   return Text(model,"state")=="retrying" || (Text(model,"state")=="paused" && Number(model,"retry_seconds")>0);
  }
  static string RetryTitle(Dictionary<string,object> model) {
   DateTimeOffset due;
   if(!DateTimeOffset.TryParse(Text(model,"retry_at"),out due)) {
    DateTimeOffset updated;
    if(!DateTimeOffset.TryParse(Text(model,"updated_at"),out updated))return "同步遇到问题 · 等待自动重试";
    due=updated.AddSeconds(Math.Min(300,Math.Max(0,Number(model,"retry_seconds"))));
   }
   int remaining=(int)Math.Min(300,Math.Max(0,Math.Ceiling((due-DateTimeOffset.Now).TotalSeconds)));
   return remaining>0?"同步遇到问题 · "+(remaining/60).ToString("00")+":"+(remaining%60).ToString("00")+" 后重试":"同步遇到问题 · 正在重试";
  }

  TrayApp(string folder,bool selfTest) {
   root=folder; test=selfTest;
   using(var stream=File.OpenRead(Path.Combine(root,"tray/Panel.xaml")))panel=(Window)XamlReader.Load(stream);
   panel.Icon=BrandImage();Find<Image>("BrandIcon").Source=panel.Icon;
   panel.Closing+=(s,e)=>{if(!exiting){e.Cancel=true;panel.Hide();}};
   panel.Deactivated+=(s,e)=>{if(!test)panel.Dispatcher.BeginInvoke((Action)(()=>{if(!activityDialogOpen && !panel.IsActive && (activityMenu==null || !activityMenu.IsOpen)){hiddenAt=DateTime.UtcNow;panel.Hide();}}),DispatcherPriority.Background);};
   Label("StatusSymbol").RenderTransform=statusRotation;
   panel.KeyDown+=(s,e)=>{if(e.Key==Key.Escape)panel.Hide();};
   Find<Button>("CloseButton").Click+=(s,e)=>panel.Hide();
   Find<Button>("FolderButton").Click+=(s,e)=>LaunchFolder();
   Find<Button>("SettingsButton").Click+=(s,e)=>OpenSettings();
   Find<Button>("SyncButton").Click+=(s,e)=>Send("sync-now");
   Find<Button>("PauseButton").Click+=(s,e)=>Send(PauseCommand());
   notify=new Forms.NotifyIcon();
   foreach(string mode in new[]{"idle","sync","paused","error"})icons[mode]=CreateIcon(mode);
   notify.Icon=icons["idle"];notify.Text="115Drive · 正在读取状态";notify.Visible=!test;
   notify.MouseClick+=(s,e)=>{if(e.Button==Forms.MouseButtons.Left)TogglePanel();};
   var menu=new Forms.ContextMenuStrip{BackColor=Drawing.Color.FromArgb(252,253,254),ForeColor=Drawing.Color.FromArgb(38,54,77)};
   menu.Items.Add("查看同步进度",null,(s,e)=>ShowPanel(false));
   menu.Items.Add("立即同步",null,(s,e)=>Send("sync-now"));
   menu.Items.Add("打开同步文件夹",null,(s,e)=>LaunchFolder());
   menu.Items.Add("在 115 中查看",null,(s,e)=>Launch("https://115.com/"));
   menu.Items.Add("设置",null,(s,e)=>OpenSettings());
   menu.Items.Add(new Forms.ToolStripSeparator());
   menu.Items.Add("退出托盘（同步继续）",null,(s,e)=>Quit());
   notify.ContextMenuStrip=menu;
   timer=new DispatcherTimer(DispatcherPriority.Background);timer.Interval=TimeSpan.FromSeconds(1);
   timer.Tick+=(s,e)=>Refresh();
   if(!test){Refresh();timer.Start();Task.Run((Action)EnsureConsole);}
  }

  string PauseCommand() {
   string mode=Text(state,"state");
   if(Object.Equals(Value(state,"requires_action"),true) && WorkerExists())return "pause";
   if(Retrying(state) && WorkerExists())return "pause";
   if(mode=="paused" || mode=="error" || !WorkerExists())return Number(state,"retry_seconds")>0?"sync-now":"resume";
   return "pause";
  }
  bool WorkerExists() {
   if(test)return true;
   var worker=Read("runtime/worker.json");
   try {
    using(var process=Process.GetProcessById((int)Number(worker,"pid"))) {
     DateTimeOffset started;
     return !process.HasExited && (process.ProcessName.StartsWith("python",StringComparison.OrdinalIgnoreCase) || process.ProcessName=="TwoDrive.Service") &&
      DateTimeOffset.TryParse(Text(worker,"started_at"),out started) && Math.Abs((process.StartTime.ToUniversalTime()-started.UtcDateTime).TotalSeconds)<30;
    }
   } catch {return false;}
  }
  void Refresh() {
   if(!test && File.Exists(Path.Combine(root,"runtime/settings-open.request")))OpenSettings();
   var model=Read("runtime/status.json");
   if(File.Exists(Path.Combine(root,"runtime/private/folder-change-active.json"))) {
    model["state"]="relocating";model["message"]=Text(Read("runtime/folder-change.json"),"message","正在更改同步文件夹，请稍候。");
   }
   Render(model,Read("runtime/progress.json"),ReadAny("runtime/activity.json"));
  }

  void Render(Dictionary<string,object> model,Dictionary<string,object> progress,object recent) {
   state=model;
   string mode=Text(model,"state"), title="正在读取同步状态", detail=Text(model,"message"), color="#EFF5FF", ink="#285793", symbol="↻", icon="sync";
   bool active=mode=="syncing" || mode=="checking" || mode=="stopping";
   bool retrying=Retrying(model);
   bool problem=mode=="error" || retrying;
   if(mode=="idle" || mode=="ready") {title="所有文件已同步";color="#EDF5EE";ink="#287B56";symbol="✓";icon="idle";detail=Text(Read("config.json"),"local_path")+"  ↔  115 网盘 /115Drive";}
   else if(mode=="relocating") {title="正在更改同步文件夹";icon="paused";symbol="Ⅱ";}
   else if(mode=="checking") {title="正在检查文件变化";detail="正在核对本地与网盘目录，传输开始后会显示进度。";}
   else if(mode=="syncing") {title="正在同步文件";}
   else if(mode=="paused") {title="同步已暂停";color="#F1F3F6";ink="#6D7887";symbol="Ⅱ";icon="paused";detail="点击恢复后校验已保存分片，继续传输剩余部分。";}
   else if(mode=="stopping") {title="正在暂停同步";detail="正在停止当前传输并保留续传分片。";}
   if(problem) {title="同步遇到问题";detail=Text(model,"message");color="#FBEEE8";ink="#AA282C";symbol="!";icon="error";}
   if(retrying) {title=RetryTitle(model);detail+="\n将自动重试；点击“立即同步”可提前重试，点击“暂停同步”可停止自动重试。";}
   bool workerMissing=!WorkerExists() && mode!="relocating" && (retrying || (mode!="paused" && !problem));
   if(workerMissing) {title="后台尚未运行";detail="点击立即同步，启动后台并检查文件变化。";symbol="Ⅱ";icon="paused";color="#F1F3F6";ink="#6D7887";active=false;}
   var files=Rows(Value(progress,"active")).ToList();
   var file=files.FirstOrDefault();
   if(mode=="paused" && !problem && file!=null && Text(file,"progress_source")=="backend" && Number(file,"size")>0 && Number(file,"bytes")>0) {
    title+=" · "+Math.Min(100,100*Number(file,"bytes")/Number(file,"size")).ToString("0",CultureInfo.InvariantCulture)+"%";
    detail=Text(file,"name")+"\n"+detail;
   }
   if(active && file!=null) {
    string direction=files.All(f=>Text(f,"direction")=="download")?"正在下载":files.All(f=>Text(f,"direction")=="upload")?"正在上传":"正在同步";
    double size=Number(file,"size"),count=Number(file,"bytes"),speed=Number(file,"speed");
    string phase=Text(file,"phase");
    bool checking=phase=="verifying",resuming=phase=="resuming",backend=Text(file,"progress_source").StartsWith("backend");
    bool confirmed=Object.Equals(Value(file,"progress_confirmed"),true);
    if(speed<=0 && !backend)speed=Number(progress,"speed");
    string percentage=size>0?Math.Min(100,100*count/size).ToString("0",CultureInfo.InvariantCulture)+"%":"";
    if(mode!="stopping") {
     if(checking) title="正在校验"+(confirmed && size>0?" · "+percentage:Number(file,"saved_bytes")>0?" · 上次 "+percentage:"");
     else title=(resuming?"正在续传":direction)+(files.Count>1?" "+files.Count.ToString(CultureInfo.InvariantCulture)+" 个文件":"")+(size>0?" · "+percentage:"")+(speed>0?" · "+Bytes(speed)+"/s":"");
    }
    detail=String.Join("\n",files.Select(f=>Text(f,"name")))+"\n"+Bytes(count)+(size>0?" / "+Bytes(size):"");
    if(backend)detail+="\n"+(checking&&!confirmed?"上次保存的进度，正在核对后确认。":resuming?"进度包含已确认的分片，仅传输剩余部分。":"进度以已确认的数据块为准。")+"\n"+Text(file,"note");
   }
   if(active && file==null && mode!="stopping") {
    string stage=Text(progress,"scan_stage"),path=Text(progress,"current_path"),count="";
    title=stage=="manifest"?"正在核对文件清单":stage=="incremental"?"正在检查目录变化":stage=="preparing"?"正在准备同步":stage=="cloud_check"?"正在检查文件夹":"正在比较文件变化";
    if(stage=="manifest" && Number(progress,"scanned_items")>0)count="已读取 "+Number(progress,"scanned_items").ToString("0")+" 个文件";
    else if((stage=="cloud_check" || stage=="preparing") && Number(progress,"scanned_dirs")>0)count="已检查 "+Number(progress,"scanned_dirs").ToString("0")+" 个";
    else if(Number(progress,"checks")>0)count="已核对 "+Number(progress,"checks").ToString("0")+" 项";
    else if(Number(progress,"listed")>0)count="已读取 "+Number(progress,"listed").ToString("0")+" 项";
    if(!String.IsNullOrEmpty(count))title+=" · "+count;
    detail=String.IsNullOrEmpty(path)?"正在核对本地与网盘目录。":path;
   }
   Label("StatusTitle").Text=title;Label("StatusSymbol").Text=symbol;
   Label("StatusSymbol").Foreground=Brush(ink);Label("StatusTitle").Foreground=Brush(ink);
   Find<Border>("StatusCard").Background=problem?Brush(color):Brushes.Transparent;
   Label("StatusTitle").ToolTip=new TextBlock{Text=title+"\n"+detail,TextWrapping=TextWrapping.Wrap,MaxWidth=360};
   bool spin=active && !problem && symbol=="↻" && SystemParameters.ClientAreaAnimation;
   if(spin!=statusSpinning) {
    statusSpinning=spin;
    statusRotation.BeginAnimation(RotateTransform.AngleProperty,spin?new DoubleAnimation(0,360,TimeSpan.FromSeconds(1.2)){RepeatBehavior=RepeatBehavior.Forever}:null);
    if(!spin)statusRotation.Angle=0;
   }
   notify.Icon=icons[icon];notify.Text=("115Drive · "+title).Substring(0,Math.Min(63,("115Drive · "+title).Length));
   string checkedAt=Text(model,"last_check_at",Text(progress,"last_check_at"));
   string syncedAt=Text(model,"last_sync_at",Text(progress,"last_sync_at"));
   string transferredAt=Text(model,"last_transfer_at",Text(progress,"last_transfer_at"));
   Label("LastSyncLabel").Text=String.IsNullOrEmpty(checkedAt)?"":"最近检查 "+TimeLabel(checkedAt);
   Label("LastSyncLabel").ToolTip="最近检查："+(String.IsNullOrEmpty(checkedAt)?"尚无记录":TimeLabel(checkedAt))+
    "\n同步成功："+(String.IsNullOrEmpty(syncedAt)?"尚无记录":TimeLabel(syncedAt))+
    "\n文件传输："+(String.IsNullOrEmpty(transferredAt)?"尚无记录":TimeLabel(transferredAt));
   bool paused=(!retrying && (mode=="paused" || (mode=="error" && !Object.Equals(Value(model,"requires_action"),true)))) || !WorkerExists();
   Label("PauseLabel").Text=paused?"恢复同步":"暂停同步";
   Label("PauseGlyph").Text=paused?"\uE768":"\uE769";
   Find<Button>("PauseButton").IsEnabled=!sending && mode!="stopping" && mode!="relocating";
   Find<Button>("SyncButton").IsEnabled=!sending && mode!="relocating" && mode!="stopping";
   var visible=Rows(recent).Reverse().Where(row=>Text(row,"action")!="backup").Take(20).ToList();
   string local=ActivityLocalRoot();
   string serialized=json.Serialize(visible)+local+DateTime.Now.ToString("yyyyMMddHHmm")+String.Join("|",visible.Select(row=>DescribeActivity(row,local).Status));
   if(serialized!=lastActivities && activityMenu==null){lastActivities=serialized;RenderActivities(visible);}
   if(!test)try {Save("runtime/tray-session.json",new{pid=Process.GetCurrentProcess().Id,updated_at=DateTimeOffset.Now.ToString("o"),icon_visible=notify.Visible,panel_visible=panel.IsVisible,title=title,worker_running=WorkerExists()});}catch{}
  }

  void RenderActivities(IEnumerable<Dictionary<string,object>> rows) {
   var list=Find<StackPanel>("ActivityList");list.Children.Clear();
   string local=ActivityLocalRoot();
   foreach(var item in rows.Where(row=>Text(row,"action")!="backup")) {
    var description=DescribeActivity(item,local);
    var grid=new Grid();
    grid.ColumnDefinitions.Add(new ColumnDefinition{Width=new GridLength(46)});grid.ColumnDefinitions.Add(new ColumnDefinition());
    var badge=new Border{Width=34,Height=44,VerticalAlignment=VerticalAlignment.Center,HorizontalAlignment=HorizontalAlignment.Left};
    var icon=ActivityIcon(Text(item,"name"));
    badge.Child=icon!=null?(UIElement)new Image{Source=icon,Width=32,Height=32,Stretch=Stretch.Uniform}:new TextBlock{Text="\uE8A5",FontFamily=new FontFamily("Segoe MDL2 Assets"),FontSize=27,Foreground=Brush("#8390A1"),VerticalAlignment=VerticalAlignment.Center};
    grid.Children.Add(badge);
    var stack=new StackPanel();Grid.SetColumn(stack,1);
    stack.Children.Add(new TextBlock{Text=FileLabel(Text(item,"name")),ToolTip=Text(item,"name"),Foreground=Brush("#26364D"),FontSize=12,TextTrimming=TextTrimming.CharacterEllipsis});
    var destination=new TextBlock{Foreground=Brush("#657083"),FontSize=11,Margin=new Thickness(0,3,0,0),TextTrimming=TextTrimming.CharacterEllipsis};
    destination.Inlines.Add(new Run(description.Status));
    if(!String.IsNullOrEmpty(description.Folder))destination.Inlines.Add(new Run(" "+description.Folder){Foreground=Brush("#2868C9")});
    stack.Children.Add(destination);
    stack.Children.Add(new TextBlock{Text=ActivityTime(Text(item,"time")),Foreground=Brush("#8390A1"),FontSize=10,Margin=new Thickness(0,2,0,0)});
    grid.Children.Add(stack);
    var button=new Button{Content=grid,Padding=new Thickness(12,10,12,10),HorizontalContentAlignment=HorizontalAlignment.Stretch,
     ToolTip=Text(item,"name")+"\n单击打开当前文件 · 右键打开所在文件夹"};
    System.Windows.Automation.AutomationProperties.SetName(button,FileLabel(Text(item,"name")));
    button.Click+=(s,e)=>OpenActivity(item,false);
    var menu=new ContextMenu();
    var open=new MenuItem{Header="打开文件"};open.Click+=(s,e)=>OpenActivity(item,false);
    var folder=new MenuItem{Header="打开所在文件夹"};folder.Click+=(s,e)=>OpenActivity(item,true);
    menu.Items.Add(folder);menu.Items.Add(open);
    menu.Opened+=(s,e)=>{activityMenu=menu;ConfigureActivityMenu(item,open,folder);};
    menu.Closed+=(s,e)=>{if(activityMenu==menu)activityMenu=null;if(!test && !activityDialogOpen && !panel.IsActive){hiddenAt=DateTime.UtcNow;panel.Hide();}};
    button.ContextMenu=menu;list.Children.Add(button);
   }
   if(list.Children.Count==0)list.Children.Add(new TextBlock{Text="暂无同步记录\n添加或修改文件后，活动会显示在这里。",Foreground=Brush("#97A2B0"),FontSize=12,Margin=new Thickness(13,24,13,12),TextWrapping=TextWrapping.Wrap,LineHeight=24});
  }

  void TogglePanel() {
   if(panel.IsVisible)panel.Hide();
   else if((DateTime.UtcNow-hiddenAt).TotalMilliseconds>220)ShowPanel(true);
  }
  void ShowPanel(bool byTray) {
   if(!test && File.Exists(Path.Combine(root,"runtime/settings-open.request"))){OpenSettings();return;}
   if(!test)Refresh();
   panel.Show();panel.UpdateLayout();
   var pointer=Forms.Cursor.Position;
   var screen=Forms.Screen.FromPoint(pointer);var area=screen.WorkingArea;
   var source=PresentationSource.FromVisual(panel);
   var transform=source==null?Matrix.Identity:source.CompositionTarget.TransformFromDevice;
   Point top=transform.Transform(new Point(area.Left,area.Top)), bottom=transform.Transform(new Point(area.Right,area.Bottom));
   Point cursor=transform.Transform(new Point(pointer.X,pointer.Y));
   double x=byTray?cursor.X-panel.ActualWidth+28:bottom.X-panel.ActualWidth-9;
   bool upper=pointer.Y<screen.Bounds.Top+screen.Bounds.Height/2;
   double y=byTray?(upper?cursor.Y+9:cursor.Y-panel.ActualHeight-9):bottom.Y-panel.ActualHeight-9;
   panel.Left=Math.Max(top.X,Math.Min(x,bottom.X-panel.ActualWidth));
   panel.Top=Math.Max(top.Y,Math.Min(y,bottom.Y-panel.ActualHeight));
   panel.Activate();
   string request=Path.Combine(root,"runtime/tray-snapshot.request");
   if(!test && File.Exists(request)) {File.Delete(request);Capture("tray-current.png");}
  }

  sealed class LocalClient:WebClient {
   public int Timeout=3000;
   public LocalClient(){Encoding=Encoding.UTF8;}
   protected override WebRequest GetWebRequest(Uri address) {var request=base.GetWebRequest(address);request.Timeout=Timeout;request.Proxy=null;return request;}
  }
  void EnsureConsole() {
   try {using(var client=new LocalClient()){client.DownloadString("http://127.0.0.1:5245/status");return;}}catch{}
   try {
    string service=Path.Combine(root,"service/TwoDrive.Service.exe");
    if(File.Exists(service)) {
     Process.Start(new ProcessStartInfo(service,"server --root \""+root+"\""){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden,WorkingDirectory=root});return;
    }
    string python=Text(Read("tray/settings.json"),"pythonw");
    if(!File.Exists(python))return;
    Process.Start(new ProcessStartInfo(python,"\""+Path.Combine(root,"setup_ui.py")+"\""){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden,WorkingDirectory=root});
   }catch(Exception error){Log(error);}
  }
  async void Send(string action) {
   lastCommand=action;if(test || sending)return;
   sending=true;Find<Button>("PauseButton").IsEnabled=false;Find<Button>("SyncButton").IsEnabled=false;
   try {
    await Task.Run(()=>{
     EnsureConsole();
     using(var client=new LocalClient()) {
      string page=client.DownloadString("http://127.0.0.1:5245/");
      var match=Regex.Match(page,"const nonce='([^']+)'");
      if(!match.Success)throw new InvalidOperationException("本机控制服务尚未就绪，请稍后再试。");
      client.Headers["X-TwoDrive-Nonce"]=match.Groups[1].Value;
      client.UploadData("http://127.0.0.1:5245/"+action,"POST",new byte[0]);
     }
    });
    Refresh();
   }catch(Exception error){Log(error);Label("StatusTitle").Text="操作未完成";Label("StatusTitle").ToolTip="本机控制服务暂时不可用，请稍后重试或打开连接与设置。";}
   finally{sending=false;Find<Button>("PauseButton").IsEnabled=true;Find<Button>("SyncButton").IsEnabled=true;}
  }
  void LaunchFolder() {Launch(Text(Read("config.json"),"local_path",Path.Combine(root,"..")));}
  void Launch(string target) {if(test){lastCommand="open";return;}try{Process.Start(new ProcessStartInfo(target){UseShellExecute=true});}catch(Exception error){Log(error);}}
  void Log(Exception error) {try{string path=Path.Combine(root,"logs/tray.log");if(File.Exists(path)&&new FileInfo(path).Length>1000000)File.Delete(path);File.AppendAllText(path,DateTime.Now.ToString("s")+" "+error.GetType().Name+": "+error.Message+Environment.NewLine,Encoding.UTF8);}catch{}}
  Drawing.Icon CreateIcon(string mode) {return CloudBrand.CreateIcon(mode);}
  static BitmapSource BrandImage() {
   using(var bitmap=CloudBrand.Render(136,"app"))using(var data=new MemoryStream()) {
    bitmap.Save(data,Drawing.Imaging.ImageFormat.Png);data.Position=0;
    var image=new BitmapImage();image.BeginInit();image.CacheOption=BitmapCacheOption.OnLoad;image.StreamSource=data;image.EndInit();image.Freeze();return image;
   }
  }
  public void Listen(string name) {ShowSignal=new EventWaitHandle(false,EventResetMode.AutoReset,name);showWait=ThreadPool.RegisterWaitForSingleObject(ShowSignal,(s,t)=>panel.Dispatcher.BeginInvoke((Action)(()=>ShowPanel(false))),null,Timeout.Infinite,false);}
  void Quit() {exiting=true;notify.Visible=false;if(settings!=null)settings.Close();panel.Close();Application.Current.Shutdown();}
  public void Dispose() {timer.Stop();if(showWait!=null)showWait.Unregister(null);if(ShowSignal!=null)ShowSignal.Dispose();notify.Visible=false;notify.Dispose();foreach(var icon in icons.Values)icon.Dispose();}

  void Capture(string filename) {
   panel.UpdateLayout();panel.Dispatcher.Invoke((Action)(()=>{}),DispatcherPriority.Render);
   var image=new RenderTargetBitmap((int)Math.Ceiling(panel.ActualWidth),(int)Math.Ceiling(panel.ActualHeight),96,96,PixelFormats.Pbgra32);
   image.Render(panel);var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(image));
   using(var file=File.Create(Path.Combine(root,"runtime/"+filename)))encoder.Save(file);
  }
  void SelfTest() {
   var passed=new List<string>();
   try {
    var fixture=Read("tests/tray-fixtures.json");
    TogglePanel();if(!panel.IsVisible)throw new Exception("Tray click did not open panel");passed.Add("tray_click_opens_panel");
    Render(Map(Value(fixture,"active")),Map(Value(fixture,"progress")),Value(fixture,"activity"));
    string transferDetail=((TextBlock)Label("StatusTitle").ToolTip).Text;
    if(!Label("StatusTitle").Text.Contains("38%") || !Label("StatusTitle").Text.Contains("2.00 MiB/s") || !transferDetail.Contains("设计稿.zip") || !transferDetail.Contains("6.00 MiB / 16.0 MiB"))throw new Exception("Numeric transfer progress mismatch");
    if(Find<StackPanel>("ActivityList").Children.Count!=2)throw new Exception("Recent activity mismatch");
    panel.UpdateLayout();
    double statusHeight=Find<Border>("StatusCard").ActualHeight;
    if(statusHeight!=24 || panel.FindName("TransferBar")!=null || panel.FindName("ScanBar")!=null)throw new Exception("Status row height or bar-free layout mismatch");
    passed.Add("file_bytes_percentage_speed_and_activity");Capture("tray-preview-active.png");
    var resumeFile=new Dictionary<string,object>{{"name","工作资料/大型归档.zip"},{"direction","upload"},{"size",67108864},{"bytes",16777216},{"saved_bytes",16777216},{"speed",0},{"phase","verifying"},{"progress_source","backend"},{"progress_confirmed",false}};
    var resumeProgress=new Dictionary<string,object>{{"active",new object[]{resumeFile}},{"speed",999999999}};
    Render(Map(Value(fixture,"active")),resumeProgress,Value(fixture,"activity"));panel.UpdateLayout();
    if(Label("StatusTitle").Text!="正在校验 · 上次 25%" || Find<Border>("StatusCard").ActualHeight!=statusHeight)throw new Exception("Saved verification progress mismatch");
    passed.Add("verification_keeps_previous_percentage_and_one_line");Capture("tray-preview-resume-checking.png");
    resumeFile["phase"]="resuming";resumeFile["progress_confirmed"]=true;
    Render(Map(Value(fixture,"active")),resumeProgress,Value(fixture,"activity"));
    if(Label("StatusTitle").Text!="正在续传 · 25%")throw new Exception("Resume restarted at zero or used local transfer speed");
    resumeFile["bytes"]=25165824;resumeFile["speed"]=2097152;
    Render(Map(Value(fixture,"active")),resumeProgress,Value(fixture,"activity"));panel.UpdateLayout();
    if(Label("StatusTitle").Text!="正在续传 · 38% · 2.00 MiB/s" || Find<Border>("StatusCard").ActualHeight!=statusHeight || Label("StatusTitle").TextWrapping!=TextWrapping.NoWrap)throw new Exception("Resumed progress layout mismatch");
    passed.Add("resuming_uses_confirmed_progress_and_preserves_height");Capture("tray-preview-resuming.png");
    resumeFile["phase"]="verifying";resumeFile["bytes"]=67108864;resumeFile["speed"]=0;
    Render(Map(Value(fixture,"active")),resumeProgress,Value(fixture,"activity"));
    if(Label("StatusTitle").Text!="正在校验 · 100%")throw new Exception("Commit verification resets completed progress");
    passed.Add("final_verification_retains_100_percent");
    resumeFile["bytes"]=16777216;resumeFile["phase"]="resuming";
    Render(Map(Value(fixture,"paused")),resumeProgress,Value(fixture,"activity"));panel.UpdateLayout();
    if(Label("StatusTitle").Text!="同步已暂停 · 25%" || Find<Border>("StatusCard").ActualHeight!=statusHeight || statusSpinning)throw new Exception("Paused checkpoint layout mismatch");
    passed.Add("paused_checkpoint_keeps_percentage_and_height");Capture("tray-preview-paused-checkpoint.png");
    Render(new Dictionary<string,object>{{"state","stopping"}},resumeProgress,Value(fixture,"activity"));
    if(Find<Button>("SyncButton").IsEnabled || Find<Button>("PauseButton").IsEnabled || Label("StatusTitle").Text!="正在暂停同步")throw new Exception("Pause transition allows premature resume");
    passed.Add("pause_transition_blocks_premature_resume");
    Render(Map(Value(fixture,"active")),Map(Value(fixture,"progress")),Value(fixture,"activity"));
    Find<Button>("PauseButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(lastCommand!="pause")throw new Exception("Pause action mismatch");passed.Add("pause_button");
    Render(Map(Value(fixture,"paused")),new Dictionary<string,object>(),Value(fixture,"activity"));
    Find<Button>("PauseButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(lastCommand!="resume")throw new Exception("Resume action mismatch");passed.Add("resume_button");Capture("tray-preview-paused.png");
    Render(Map(Value(fixture,"error")),new Dictionary<string,object>(),Value(fixture,"activity"));if(Label("StatusTitle").Text!="同步遇到问题")throw new Exception("Error hidden");passed.Add("error_state");Capture("tray-preview-error.png");
    var retry=new Dictionary<string,object>{{"state","retrying"},{"message","网络连接暂时不可用"},{"retry_seconds",30},{"retry_at",DateTimeOffset.Now.AddSeconds(30).ToString("o")}};
    Render(retry,resumeProgress,Value(fixture,"activity"));panel.UpdateLayout();
    if(!Label("StatusTitle").Text.Contains("00:30 后重试") || Label("PauseLabel").Text!="暂停同步" || Find<Border>("StatusCard").ActualHeight!=statusHeight)throw new Exception("Retry countdown or fixed row mismatch");
    Find<Button>("PauseButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(lastCommand!="pause")throw new Exception("Retry cannot be paused");
    Find<Button>("SyncButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(lastCommand!="sync-now")throw new Exception("Retry cannot be requested immediately");
    passed.Add("retry_countdown_pause_and_immediate_retry");Capture("tray-preview-retrying.png");
    retry["retry_at"]=DateTimeOffset.Now.AddSeconds(12).ToString("o");Render(retry,new Dictionary<string,object>(),null);
    if(!Label("StatusTitle").Text.Contains("00:12 后重试"))throw new Exception("Retry countdown does not advance");
    retry["retry_at"]=DateTimeOffset.Now.AddSeconds(-1).ToString("o");Render(retry,new Dictionary<string,object>(),null);
    if(Label("StatusTitle").Text!="同步遇到问题 · 正在重试")throw new Exception("Expired retry shows negative time");
    retry["state"]="paused";retry.Remove("retry_at");retry["updated_at"]=DateTimeOffset.Now.AddSeconds(-10).ToString("o");Render(retry,new Dictionary<string,object>(),null);
    if(!Label("StatusTitle").Text.Contains("00:20 后重试") || PauseCommand()!="pause")throw new Exception("Legacy worker retry state mismatch");
    passed.Add("retry_countdown_expiry_and_legacy_worker");
    Render(Map(Value(fixture,"checking")),Map(Value(fixture,"scan_progress")),Value(fixture,"activity"));panel.UpdateLayout();
    if(!Label("StatusTitle").Text.Contains("7") || !((TextBlock)Label("StatusTitle").ToolTip).Text.Contains("图片") || Find<Border>("StatusCard").ActualHeight!=statusHeight || (SystemParameters.ClientAreaAnimation&&!statusSpinning))throw new Exception("Fixed-height animated scan status mismatch");
    passed.Add("scan_spinner_and_fixed_status_height");Capture("tray-preview-checking.png");
    Render(Map(Value(fixture,"idle")),new Dictionary<string,object>(),Value(fixture,"activity"));panel.UpdateLayout();
    if(Find<Border>("StatusCard").ActualHeight!=statusHeight || statusSpinning || statusRotation.HasAnimatedProperties)throw new Exception("Idle status height or animation mismatch");
    passed.Add("idle_same_height_and_animation_stops");Capture("tray-preview-idle.png");
    var timeState=new Dictionary<string,object>{{"state","idle"},{"last_check_at",DateTimeOffset.Now.ToString("o")},{"last_sync_at",DateTimeOffset.Now.AddHours(-1).ToString("o")},{"last_transfer_at",DateTimeOffset.Now.AddHours(-2).ToString("o")}};
    Render(timeState,new Dictionary<string,object>(),Value(fixture,"activity"));panel.UpdateLayout();
    string timeDetail=Convert.ToString(Label("LastSyncLabel").ToolTip);
    if(!Label("LastSyncLabel").Text.StartsWith("最近检查 ") || !timeDetail.Contains("同步成功：") || !timeDetail.Contains("文件传输：") || timeDetail.Contains("尚无记录"))throw new Exception("Check/sync/transfer times were conflated");
    passed.Add("independent_check_sync_transfer_times");Capture("tray-preview-times.png");
    Render(new Dictionary<string,object>{{"state","error"},{"message","云端存在重复名称"},{"requires_action",true}},new Dictionary<string,object>(),null);
    if(Label("StatusTitle").Text!="同步遇到问题" || Label("PauseLabel").Text!="暂停同步" || PauseCommand()!="pause")throw new Exception("Action-required state cannot pause for reauthorization");
    passed.Add("action_required_waits_for_explicit_retry");
    TestActivityActions(passed);
    RenderActivities(Rows(Value(fixture,"activity")).Reverse());
    Find<Button>("SyncButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(lastCommand!="sync-now")throw new Exception("Sync action mismatch");passed.Add("sync_now_button");
    TogglePanel();if(panel.IsVisible)throw new Exception("Second tray click did not hide panel");passed.Add("second_click_hides_panel");
    TestNativeSettings(passed);
    Save("runtime/tray-ui-test.json",new{complete=true,passed=passed,time=DateTimeOffset.Now.ToString("o")});
   }catch(Exception error){Save("runtime/tray-ui-test.json",new{complete=false,passed=passed,error=error.ToString()});Environment.ExitCode=1;}
   Quit();
  }

  [STAThread] public static int Main(string[] args) {
   string root=File.Exists(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"tray/Panel.xaml"))?AppDomain.CurrentDomain.BaseDirectory:Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"../.."));
   int index=Array.IndexOf(args,"--root");if(index>=0&&index+1<args.Length)root=Path.GetFullPath(args[index+1]);
   root=Path.GetFullPath(root);if(root.Length>Path.GetPathRoot(root).Length)root=root.TrimEnd('\\','/');
   Directory.CreateDirectory(Path.Combine(root,"runtime"));Directory.CreateDirectory(Path.Combine(root,"logs"));
   bool test=args.Contains("--self-test"),show=args.Contains("--show"),showSettings=args.Contains("--settings");
   if(showSettings)File.WriteAllText(Path.Combine(root,"runtime/settings-open.request"),"");
   if(args.Contains("--settings-snapshot"))File.WriteAllText(Path.Combine(root,"runtime/settings-snapshot.request"),"");
   if(!test && args.Contains("--snapshot"))File.WriteAllText(Path.Combine(root,"runtime/tray-snapshot.request"),"");
   string suffix;using(var hash=SHA256.Create())suffix=BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(root.ToLowerInvariant()))).Replace("-","").Substring(0,16);
   string name="Local\\TwoDriveTray-"+suffix;bool first;
   using(var mutex=new Mutex(true,name+(test?"-test":""),out first)) {
    if(!first){if(show||showSettings)try{using(var signal=EventWaitHandle.OpenExisting(name+"-show"))signal.Set();}catch{}return 0;}
    var application=new Application{ShutdownMode=ShutdownMode.OnExplicitShutdown};
    try {
     using(var tray=new TrayApp(root,test)) {
      application.MainWindow=tray.panel;tray.Listen(name+(test?"-test":"")+"-show");
      application.Startup+=(s,e)=>{if(test)tray.panel.Dispatcher.BeginInvoke((Action)tray.SelfTest);else if(showSettings||!File.Exists(Path.Combine(root,"runtime/private/initialized.json")))tray.OpenSettings();else if(show)tray.ShowPanel(false);};
      application.Run();
     }
    }catch(Exception error){File.WriteAllText(Path.Combine(root,"runtime/tray-startup-error.txt"),error.ToString());return 1;}
   }
   return Environment.ExitCode;
  }
 }
}
