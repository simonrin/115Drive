using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms=System.Windows.Forms;

namespace TwoDrive {
 sealed partial class TrayApp {
  Window settings;
  DispatcherTimer settingsTimer;
  bool settingsBusy,settingsPolling,renderingSettings,settingsInitialized;
  string previewTarget="",qrKey="",settingsError="",folderResultVersion="";
  T SF<T>(string name) where T:class {return settings.FindName(name) as T;}
  void Notice(string message) {SF<TextBlock>("SettingsNotice").Text=message;}

  Dictionary<string,object> DesktopInfo(LocalClient client) {
   var serializer=new JavaScriptSerializer();
   var info=Map(serializer.DeserializeObject(client.DownloadString("http://127.0.0.1:5245/desktop")));
   if(!String.Equals(Path.GetFullPath(Text(info,"root")).TrimEnd('\\','/'),Path.GetFullPath(root).TrimEnd('\\','/'),StringComparison.OrdinalIgnoreCase))
    throw new InvalidOperationException("另一份 115Drive 正在运行，请先关闭另一份程序的后台。");
   return info;
  }
  Dictionary<string,object> DesktopRequest(string route,object body=null) {
   using(var client=new LocalClient{Timeout=60000}) {
    try {
     var info=DesktopInfo(client);
     if(route=="desktop")return info;
     string text;
     if(body==null)text=client.DownloadString("http://127.0.0.1:5245/"+route);
     else {
      client.Headers["X-TwoDrive-Nonce"]=Text(info,"nonce");client.Headers["Content-Type"]="application/json";
      text=client.UploadString("http://127.0.0.1:5245/"+route,"POST",new JavaScriptSerializer().Serialize(body));
     }
     try{return Map(new JavaScriptSerializer().DeserializeObject(text));}catch{return new Dictionary<string,object>{{"message",text}};}
    }catch(WebException error) {
     if(error.Response!=null)using(var stream=error.Response.GetResponseStream())using(var reader=new StreamReader(stream,Encoding.UTF8)) {
      string text=reader.ReadToEnd();
      try{text=Text(Map(new JavaScriptSerializer().DeserializeObject(text)),"error",text);}catch{}
      throw new InvalidOperationException(text);
     }
     throw new InvalidOperationException("本机后台正在启动或暂时不可用，请稍后重试。");
    }
   }
  }
  void OpenSettings() {
   string request=Path.Combine(root,"runtime/settings-open.request");if(!test && File.Exists(request))File.Delete(request);
   panel.Hide();
   if(settings==null) {
    using(var stream=File.OpenRead(Path.Combine(root,"tray/Settings.xaml")))settings=(Window)XamlReader.Load(stream);
    settings.Closed+=(s,e)=>{if(settingsTimer!=null)settingsTimer.Stop();settings=null;};
    SF<Button>("SyncNav").Click+=(s,e)=>SettingsPage("Sync");
    SF<Button>("AccountNav").Click+=(s,e)=>SettingsPage("Account");
    SF<Button>("AboutNav").Click+=(s,e)=>SettingsPage("About");
    SF<Button>("SettingsOpenFolder").Click+=(s,e)=>LaunchFolder();
    SF<Button>("OpenRecoveryButton").Content="查看历史恢复目录";
    SF<Button>("OpenRecoveryButton").IsEnabled=Directory.Exists(Path.Combine(root,"recovery"));
    SF<Button>("OpenRecoveryButton").Click+=(s,e)=>{if(Directory.Exists(Path.Combine(root,"recovery")))Launch(Path.Combine(root,"recovery"));};
    SF<Button>("ChangeFolderButton").Click+=(s,e)=>{SF<StackPanel>("FolderEditor").Visibility=Visibility.Visible;SF<TextBox>("NewFolderPath").Focus();};
    SF<Button>("BrowseFolderButton").Click+=(s,e)=>BrowseLocalFolder();
    SF<TextBox>("NewFolderPath").TextChanged+=(s,e)=>{previewTarget="";SF<Button>("ApplyFolderButton").IsEnabled=false;};
    SF<Button>("CheckFolderButton").Click+=(s,e)=>PreviewLocalFolder();
    SF<Button>("ApplyFolderButton").Click+=(s,e)=>ApplyLocalFolder();
    SF<Button>("CancelFolderButton").Click+=(s,e)=>SettingsCommand("folder/cancel");
    SF<Button>("SettingsPauseButton").Click+=(s,e)=>SettingsCommand(PauseCommand());
    SF<Button>("NativeLoginButton").Click+=(s,e)=>SettingsCommand("qr/start");
    SF<Button>("NativeCancelLogin").Click+=(s,e)=>SettingsCommand("qr/cancel");
    SF<Button>("PauseForLogin").Click+=(s,e)=>SettingsCommand("pause");
    SF<CheckBox>("StartupCheck").Click+=(s,e)=>{if(!renderingSettings)SettingsCommand("desktop/preferences",new{start_with_windows=SF<CheckBox>("StartupCheck").IsChecked==true});};
    settingsTimer=new DispatcherTimer{Interval=TimeSpan.FromSeconds(2)};
    settingsTimer.Tick+=(s,e)=>RefreshSettings();
   }
   settings.Show();settings.Activate();SettingsPage("Sync");
   if(test)RenderSettings(new Dictionary<string,object>{{"local_path",Text(Read("config.json"),"local_path")},{"initialized",true},{"start_with_windows",true}},new Dictionary<string,object>{{"state","idle"},{"message","同步正常"}},new Dictionary<string,object>());
   else {settingsTimer.Start();RefreshSettings();}
  }
  void SettingsPage(string page) {
   foreach(string name in new[]{"Sync","Account","About"}) {
    SF<StackPanel>(name+"Page").Visibility=name==page?Visibility.Visible:Visibility.Collapsed;
    SF<Button>(name+"Nav").Background=Brush(name==page?"#E2EBFA":"#F4F6FA");
   }
   SF<TextBlock>("SettingsTitle").Text=page=="Sync"?"同步与备份":page=="Account"?"账户":"关于";
   SF<TextBlock>("SettingsSubtitle").Text=page=="Sync"?"管理这台电脑上的文件与同步偏好":page=="Account"?"连接你的 115 网盘":"115Drive · 自动双向同步";
  }
  async void RefreshSettings() {
   if(settings==null||settingsPolling||settingsBusy)return;
   settingsPolling=true;
   try {
    var data=await Task.Run(()=>{
     var info=DesktopRequest("desktop");var status=DesktopRequest("status");var qr=DesktopRequest("qr/status");
     byte[] bytes=null;string image=Text(qr,"image");
     if(image.StartsWith("/qr/image?key=",StringComparison.Ordinal)&&image!=qrKey)using(var client=new LocalClient()){bytes=client.DownloadData("http://127.0.0.1:5245"+image);}
     return new object[]{info,status,qr,bytes};
    });
    if(settings==null)return;
    RenderSettings(Map(data[0]),Map(data[1]),Map(data[2]));
    if(data[3]!=null) {
     var bitmap=new BitmapImage();using(var stream=new MemoryStream((byte[])data[3])){bitmap.BeginInit();bitmap.CacheOption=BitmapCacheOption.OnLoad;bitmap.StreamSource=stream;bitmap.EndInit();bitmap.Freeze();}
     SF<Image>("LoginQrImage").Source=bitmap;qrKey=Text(Map(data[2]),"image");
    }
    string request=Path.Combine(root,"runtime/settings-snapshot.request");
    if(File.Exists(request)){File.Delete(request);CaptureSettings("settings-current.png");}
   }catch(Exception error){if(settings!=null)Notice(error.Message);}
   finally{settingsPolling=false;}
  }
  void RenderSettings(Dictionary<string,object> info,Dictionary<string,object> status,Dictionary<string,object> qr) {
   renderingSettings=true;
   settingsInitialized=Value(info,"initialized") as bool? ?? false;
   SF<TextBlock>("LocalFolderLabel").Text=Text(info,"local_path");
   SF<TextBlock>("LocalFolderLabel").ToolTip=Text(info,"local_path");
   SF<CheckBox>("StartupCheck").IsChecked=Value(info,"start_with_windows") as bool? ?? true;
   SF<TextBlock>("DataFolderLabel").Text="程序与数据位置：\n"+root;
   SF<TextBlock>("SyncStateLabel").Text=Text(status,"state")=="idle"?"文件已同步":Text(status,"message");
   SF<Button>("SettingsPauseButton").Content=Text(status,"state")=="paused"||Text(status,"state")=="error"?"恢复同步":"暂停同步";
   SF<Button>("SettingsPauseButton").IsEnabled=settingsInitialized&&!settingsBusy;
   SF<TextBlock>("AccountStateLabel").Text=settingsInitialized?"已连接 · 双向同步 /115Drive":"尚未连接，先确认同步位置，再扫码登录。";
   bool authAllowed=Value(status,"authorization_allowed") as bool? ?? !settingsInitialized;
   string qrState=Text(qr,"state");bool connecting=qrState=="generating"||qrState=="waiting"||qrState=="scanned"||qrState=="connecting";
   SF<Button>("NativeLoginButton").IsEnabled=authAllowed&&!connecting;
   SF<Button>("NativeLoginButton").Content=settingsInitialized?"重新扫码连接":"扫码连接";
   SF<Button>("PauseForLogin").Visibility=settingsInitialized&&!authAllowed?Visibility.Visible:Visibility.Collapsed;
   SF<Button>("NativeCancelLogin").Visibility=(Value(qr,"cancellable") as bool? ?? false)?Visibility.Visible:Visibility.Collapsed;
   SF<Image>("LoginQrImage").Visibility=String.IsNullOrEmpty(Text(qr,"image"))?Visibility.Collapsed:Visibility.Visible;
   if(String.IsNullOrEmpty(Text(qr,"image"))){SF<Image>("LoginQrImage").Source=null;qrKey="";}
   SF<TextBlock>("LoginMessage").Text=connecting||!settingsInitialized?Text(qr,"message"):authAllowed?"同步已暂停，可以重新连接。":"重新连接前请先暂停同步。";
   SF<Button>("CheckFolderButton").Content=settingsInitialized?"检查目标":"检查并保存位置";
   var change=Map(Value(status,"folder_change"));bool moving=Value(status,"folder_changing") as bool? ?? false;
   foreach(string name in new[]{"ChangeFolderButton","BrowseFolderButton","CheckFolderButton"})SF<Button>(name).IsEnabled=!moving&&!settingsBusy;
   SF<TextBox>("NewFolderPath").IsEnabled=!moving&&!settingsBusy;
   SF<CheckBox>("StartupCheck").IsEnabled=!moving&&!settingsBusy;
   string changeVersion=new JavaScriptSerializer().Serialize(change);
   if(moving||((Text(change,"state")=="complete"||Text(change,"state")=="error"||Text(change,"state")=="cancelled")&&changeVersion!=folderResultVersion)) {
    if(moving)SF<StackPanel>("FolderEditor").Visibility=Visibility.Visible;
    if(String.IsNullOrEmpty(previewTarget))SF<TextBlock>("FolderResult").Text=Text(change,"message")+(moving?"\n"+Text(change,"name"):"");
   }
   folderResultVersion=changeVersion;
   SF<ProgressBar>("FolderProgress").Visibility=moving?Visibility.Visible:Visibility.Collapsed;
   SF<ProgressBar>("FolderProgress").IsIndeterminate=Number(change,"total_bytes")<=0;
   SF<ProgressBar>("FolderProgress").Maximum=Math.Max(1,Number(change,"total_bytes"));SF<ProgressBar>("FolderProgress").Value=Number(change,"bytes");
   SF<Button>("CancelFolderButton").Visibility=moving&&(Value(change,"cancellable") as bool? ?? false)?Visibility.Visible:Visibility.Collapsed;
   if(moving)SF<Button>("ApplyFolderButton").IsEnabled=false;
   Notice(!String.IsNullOrEmpty(settingsError)?settingsError:moving?"正在安全更改文件夹，等待完成或取消。":"设置自动保存 · 云端同步范围 /115Drive");
   renderingSettings=false;
  }
  void BrowseLocalFolder() {
   if(test){SF<TextBox>("NewFolderPath").Text="D:\\115Drive";return;}
   using(var dialog=new Forms.FolderBrowserDialog{Description="选择新的同步文件夹（空文件夹）",ShowNewFolderButton=true}) {
    dialog.SelectedPath=SF<TextBlock>("LocalFolderLabel").Text;
    if(dialog.ShowDialog(new NativeOwner(new System.Windows.Interop.WindowInteropHelper(settings).Handle))==Forms.DialogResult.OK)SF<TextBox>("NewFolderPath").Text=dialog.SelectedPath;
   }
  }
  sealed class NativeOwner:Forms.IWin32Window {public IntPtr Handle{get;private set;}public NativeOwner(IntPtr handle){Handle=handle;}}
  async void PreviewLocalFolder() {
   string path=SF<TextBox>("NewFolderPath").Text.Trim();
   if(test){lastCommand="folder/preview";previewTarget=path;SF<Button>("ApplyFolderButton").IsEnabled=true;return;}
   if(settingsBusy)return;settingsBusy=true;settingsError="";
   SF<Button>("CheckFolderButton").IsEnabled=false;SF<Button>("ApplyFolderButton").IsEnabled=false;
   SF<TextBlock>("FolderResult").Text="正在检查文件夹与可用空间…";
   try {
    if(!settingsInitialized) {
     await Task.Run(()=>DesktopRequest("desktop/initial-folder",new{path=path}));
     if(settings!=null){SF<TextBlock>("FolderResult").Text="位置已保存。请到“账户”扫码连接。";Notice("本地位置已保存，尚未开始同步。");}
    }else {
     var result=await Task.Run(()=>DesktopRequest("folder/preview",new{path=path}));
     if(settings==null)return;
     if(SF<TextBox>("NewFolderPath").Text.Trim()!=path)throw new InvalidOperationException("路径已更改，请重新检查目标。");
     previewTarget=Text(result,"target");
     SF<TextBlock>("FolderResult").Text="目标："+previewTarget+"\n共 "+Number(result,"files").ToString("0")+" 个文件 · "+Bytes(Number(result,"bytes"))+"\n可用空间 "+Bytes(Number(result,"free_bytes"))+"。旧目录将保留。";
     SF<Button>("ApplyFolderButton").IsEnabled=true;
    }
   }catch(Exception error){if(settings!=null)SF<TextBlock>("FolderResult").Text=error.Message;}
   finally{settingsBusy=false;if(settings!=null)SF<Button>("CheckFolderButton").IsEnabled=true;}
  }
  void ApplyLocalFolder() {if(String.IsNullOrEmpty(previewTarget))return;string target=previewTarget;previewTarget="";SettingsCommand("folder/change",new{path=target});}
  async void SettingsCommand(string route,object body=null) {
   lastCommand=route;if(test)return;if(settingsBusy)return;settingsBusy=true;settingsError="";Notice("正在处理…");
   SF<Button>("ApplyFolderButton").IsEnabled=false;SF<CheckBox>("StartupCheck").IsEnabled=false;
   try {await Task.Run(()=>DesktopRequest(route,body??new{}));if(settings!=null)Notice("已提交，正在更新状态…");}
   catch(Exception error){settingsError=error.Message;if(settings!=null)Notice(error.Message);}
   finally {settingsBusy=false;}
  }
  void CaptureSettings(string name) {
   settings.UpdateLayout();settings.Dispatcher.Invoke((Action)(()=>{}),DispatcherPriority.Render);
   var content=(FrameworkElement)settings.Content;
   var bitmap=new RenderTargetBitmap((int)content.ActualWidth,(int)content.ActualHeight,96,96,PixelFormats.Pbgra32);bitmap.Render(content);
   var png=new PngBitmapEncoder();png.Frames.Add(BitmapFrame.Create(bitmap));using(var file=File.Create(Path.Combine(root,"runtime/"+name)))png.Save(file);
  }
  void TestNativeSettings(List<string> passed) {
   Find<Button>("SettingsButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(settings==null||!settings.IsVisible)throw new Exception("Native settings did not open");passed.Add("tray_gear_opens_native_settings");
   SF<Button>("ChangeFolderButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   SF<Button>("BrowseFolderButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   SF<Button>("CheckFolderButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="folder/preview"||!SF<Button>("ApplyFolderButton").IsEnabled)throw new Exception("Native folder preview wiring failed");
   SF<Button>("ApplyFolderButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(lastCommand!="folder/change")throw new Exception("Folder apply wiring failed");passed.Add("native_folder_preview_and_apply");
   SF<Button>("AccountNav").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(SF<StackPanel>("AccountPage").Visibility!=Visibility.Visible)throw new Exception("Account navigation failed");passed.Add("native_account_page");
   SF<Button>("AboutNav").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));if(SF<StackPanel>("AboutPage").Visibility!=Visibility.Visible)throw new Exception("About navigation failed");passed.Add("native_about_page");
   SettingsPage("Sync");SF<StackPanel>("FolderEditor").Visibility=Visibility.Collapsed;CaptureSettings("settings-preview.png");settings.Close();
  }
 }
}
