using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Interop;
using System.Windows.Threading;
using Forms=System.Windows.Forms;

namespace TwoDrive {
 sealed partial class TrayApp {
  static bool InFolder(string path,string folder) {
   return path.Equals(folder,StringComparison.OrdinalIgnoreCase)||path.StartsWith(folder+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase);
  }
  static string ActivityPath(string local,string relative) {
   try {
    if(String.IsNullOrWhiteSpace(local)||String.IsNullOrWhiteSpace(relative)||!Path.IsPathRooted(local))return null;
    local=Path.GetFullPath(local).TrimEnd('\\','/');
    relative=relative.Replace('/','\\');
    if(Path.IsPathRooted(relative)||relative.Contains(":"))return null;
    var parts=relative.Split('\\');
    if(parts.Any(p=>String.IsNullOrEmpty(p)||p=="."||p==".."||p.EndsWith(".")||p.EndsWith(" ")||p.IndexOfAny(Path.GetInvalidFileNameChars())>=0))return null;
    string path=Path.GetFullPath(Path.Combine(local,relative));
    if(!InFolder(path,local))return null;
    // A history entry must not turn into a URL, shell argument or junction escape.
    for(string parent=path;parent!=null && InFolder(parent,local);parent=Path.GetDirectoryName(parent)) {
     if((File.Exists(parent)||Directory.Exists(parent))&&(File.GetAttributes(parent)&FileAttributes.ReparsePoint)!=0)return null;
    }
    return path;
   }catch{return null;}
  }
  void ActivityNotice(string message) {
   lastCommand="activity-unavailable";lastActivityTarget="";
   if(!test) {
    ActivityTrace(new{action=lastCommand,success=false,dialog=true,time=DateTimeOffset.Now.ToString("o")});
    activityDialogOpen=true;
    try {panel.Show();MessageBox.Show(panel,message,"115Drive",MessageBoxButton.OK,MessageBoxImage.Information);}
    finally {activityDialogOpen=false;}
   }
  }
  void ActivityTrace(object value) {if(!test)try{Save("runtime/activity-action-last.json",value);}catch{}}
  sealed class ActivityTarget {
   public string File,Folder,Message;
  }
  string ActivityLocalRoot() {return test&&!String.IsNullOrEmpty(activityTestRoot)?activityTestRoot:Text(Read("config.json"),"local_path");}
  sealed class ActivityPresentation {
   public string Status,Folder;
  }
  ActivityPresentation DescribeActivity(Dictionary<string,object> item,string local) {
   string path=ActivityPath(local,Text(item,"name")),action=Text(item,"action");
   if(!Directory.Exists(local))return new ActivityPresentation{Status="同步文件夹暂不可访问"};
   if(path==null)return new ActivityPresentation{Status="无法定位此文件"};
   if(!File.Exists(path)||action=="deleted")return new ActivityPresentation{Status="已移动或删除"};
   return new ActivityPresentation{Status=action=="download"?"已下载到":action=="upload"?"已上传到":"已同步到",
    Folder=FileLabel(Path.GetDirectoryName(path))};
  }
  static string ActivityTime(string value) {
   DateTimeOffset time;if(!DateTimeOffset.TryParse(value,out time))return "";
   double seconds=Math.Max(0,(DateTimeOffset.Now-time).TotalSeconds);
   if(seconds<60)return "刚刚";
   if(seconds<3600)return ((int)(seconds/60)).ToString()+" 分钟前";
   if(seconds<86400)return ((int)(seconds/3600)).ToString()+" 小时前";
   return time.LocalDateTime.ToString("MM/dd HH:mm");
  }
  readonly Dictionary<string,ImageSource> activityIcons=new Dictionary<string,ImageSource>(StringComparer.OrdinalIgnoreCase);
  [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]
  struct ShellFileInfo {
   public IntPtr Icon; public int Index; public uint Attributes;
   [MarshalAs(UnmanagedType.ByValTStr,SizeConst=260)] public string DisplayName;
   [MarshalAs(UnmanagedType.ByValTStr,SizeConst=80)] public string TypeName;
  }
  [DllImport("shell32.dll",CharSet=CharSet.Unicode)] static extern IntPtr SHGetFileInfo(string path,uint attributes,out ShellFileInfo info,uint size,uint flags);
  ImageSource ActivityIcon(string name) {
   try {
    string extension=Path.GetExtension(name);ImageSource image;
    if(activityIcons.TryGetValue(extension,out image))return image;
    ShellFileInfo info;
    // Resolve by extension only: no file reads, thumbnails or executable loading.
    SHGetFileInfo("file"+extension,128,out info,(uint)Marshal.SizeOf(typeof(ShellFileInfo)),0x100|0x10);
    if(info.Icon==IntPtr.Zero)return null;
    try {image=Imaging.CreateBitmapSourceFromHIcon(info.Icon,Int32Rect.Empty,System.Windows.Media.Imaging.BitmapSizeOptions.FromEmptyOptions());image.Freeze();}
    finally {DestroyIcon(info.Icon);}
    if(activityIcons.Count>=64)activityIcons.Clear();
    activityIcons[extension]=image;return image;
   }catch{return null;}
  }
  ActivityTarget ResolveActivity(Dictionary<string,object> item) {
   var result=new ActivityTarget{Message="文件已移动或删除，本机原位置没有可打开的文件。可右键打开仍存在的上级文件夹。"};
   string local=ActivityLocalRoot(),name=Text(item,"name"),path=ActivityPath(local,name);
   if(path==null){result.Message="这条活动无法定位到同步文件夹中的文件。";return result;}
   if(File.Exists(path))result.File=path;
   if(result.File!=null)result.Folder=Path.GetDirectoryName(result.File);
   if(result.Folder==null) {
    string boundary=Path.GetFullPath(local).TrimEnd('\\','/'),folder=Path.GetDirectoryName(path);
    while(folder!=null && InFolder(folder,boundary) && !Directory.Exists(folder))folder=Path.GetDirectoryName(folder);
    if(folder!=null && InFolder(folder,boundary) && Directory.Exists(folder))result.Folder=folder;
   }
   return result;
  }
  void ConfigureActivityMenu(Dictionary<string,object> item,MenuItem open,MenuItem folder) {
   try {
    var target=ResolveActivity(item);
    open.IsEnabled=target.File!=null;
    open.Header=target.File==null?"已移动或删除":"打开文件";
    open.ToolTip=target.File??target.Message;
    folder.IsEnabled=target.Folder!=null;folder.ToolTip=target.Folder??"同步文件夹暂时不可访问";
   }catch(Exception error){Log(error);open.IsEnabled=false;folder.IsEnabled=false;}
  }
  [DllImport("shell32.dll",CharSet=CharSet.Unicode)] static extern IntPtr ILCreateFromPath(string path);
  [DllImport("shell32.dll")] static extern void ILFree(IntPtr pidl);
  [DllImport("shell32.dll")] static extern int SHOpenFolderAndSelectItems(IntPtr pidl,uint count,IntPtr children,uint flags);
  void RevealActivityFile(string file) {
   IntPtr item=ILCreateFromPath(file);
   if(item==IntPtr.Zero)throw new IOException("无法定位文件。");
   try {Marshal.ThrowExceptionForHR(SHOpenFolderAndSelectItems(item,0,IntPtr.Zero,0));}
   finally {ILFree(item);}
  }
  void OpenActivity(Dictionary<string,object> item,bool folder) {
   ActivityTrace(new{action=folder?"request-folder":"request-file",success=false,time=DateTimeOffset.Now.ToString("o")});
   try {
    var resolved=ResolveActivity(item);
    string target=folder?resolved.Folder:resolved.File;
    if(target==null){ActivityNotice(folder?"同步文件夹暂时不可访问。":resolved.Message);return;}
    lastCommand=folder?"activity-open-folder":"activity-open-file";lastActivityTarget=target;
    if(test)return;
    if(folder && resolved.File!=null)RevealActivityFile(resolved.File);
    else Process.Start(new ProcessStartInfo(target){UseShellExecute=true,WorkingDirectory=folder?target:Path.GetDirectoryName(target)});
    ActivityTrace(new{action=lastCommand,success=true,time=DateTimeOffset.Now.ToString("o")});
    panel.Hide();
   }catch(Exception error){Log(error);ActivityNotice("无法打开此文件，请检查文件是否存在以及默认打开方式。");}
  }

  void TestActivityActions(List<string> passed) {
   string fixture=Path.Combine(root,"runtime","activity-ui-test-"+Guid.NewGuid().ToString("N"));
   activityTestRoot=Path.Combine(fixture,"local");
   string relative="资料 子目录/hello,测试.txt";
   string file=Path.Combine(activityTestRoot,relative.Replace('/','\\'));
   Directory.CreateDirectory(Path.GetDirectoryName(file));File.WriteAllText(file,"115Drive native activity test");
   var entry=new Dictionary<string,object>{{"name",relative},{"action","download"}};
   RenderActivities(new[]{entry});panel.UpdateLayout();
   var button=(Button)Find<StackPanel>("ActivityList").Children[0];
   button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="activity-open-file"||lastActivityTarget!=file)throw new Exception("Activity click resolved the wrong file");
   passed.Add("activity_click_opens_unicode_nested_file");
   button.ContextMenu.PlacementTarget=button;button.ContextMenu.IsOpen=true;
   panel.Dispatcher.Invoke((Action)(()=>{}),DispatcherPriority.Render);
   if(!button.ContextMenu.IsOpen||!panel.IsVisible)throw new Exception("Activity context menu did not stay open");
   ((MenuItem)button.ContextMenu.Items[0]).RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
   if(lastCommand!="activity-open-folder"||lastActivityTarget!=Path.GetDirectoryName(file))throw new Exception("Activity folder action resolved the wrong directory");
   button.ContextMenu.IsOpen=false;
   passed.Add("activity_context_menu_opens_containing_folder");
   OpenActivity(new Dictionary<string,object>{{"name","missing/sub/file.txt"}},false);
   if(lastCommand!="activity-unavailable")throw new Exception("Missing activity file was opened");
   OpenActivity(new Dictionary<string,object>{{"name","missing/sub/file.txt"}},true);
   if(lastActivityTarget!=activityTestRoot)throw new Exception("Missing folder fallback escaped the sync root");
   passed.Add("activity_missing_file_and_parent_fallback");
   string recovered=Path.Combine(fixture,"recovery","run","archived.txt");
   Directory.CreateDirectory(Path.GetDirectoryName(recovered));File.WriteAllText(recovered,"Recovered test file");
   var archived=new Dictionary<string,object>{{"name","archived.txt"},{"action","backup"}};
   OpenActivity(archived,false);
   if(lastCommand!="activity-unavailable")throw new Exception("Missing file silently opened an obsolete recovery version");
   passed.Add("activity_missing_file_does_not_open_old_recovery");
   var description=DescribeActivity(entry,activityTestRoot);
   if(description.Status!="已下载到"||description.Folder!="资料 子目录")throw new Exception("Download folder description mismatch");
   var content=(StackPanel)((Grid)button.Content).Children[1];
   if(content.Children.Count!=3)throw new Exception("Activity must have filename, destination and time on separate lines");
   File.Delete(file);
   if(DescribeActivity(entry,activityTestRoot).Status!="已移动或删除")throw new Exception("Moved/deleted file status did not update");
   File.WriteAllText(file,"115Drive native activity test");
   if(DescribeActivity(entry,activityTestRoot).Status!="已下载到")throw new Exception("Restored file status did not update");
   passed.Add("activity_three_lines_destination_and_live_missing_status");
   RenderActivities(new[]{archived,entry});
   if(Find<StackPanel>("ActivityList").Children.Count!=1)throw new Exception("Internal recovery history leaked into recent activities");
   passed.Add("activity_hides_internal_recovery_history");
   foreach(string invalid in new[]{"../outside.txt","/outside.txt","C:\\outside.txt","https://example.com","file.txt:stream","a/../../outside","a\\..\\outside"}) {
    if(ActivityPath(activityTestRoot,invalid)!=null)throw new Exception("Unsafe activity path was accepted");
   }
   passed.Add("activity_paths_cannot_escape_sync_root");
   activityTestRoot=Path.Combine(fixture,"relocated");
   string moved=Path.Combine(activityTestRoot,relative.Replace('/','\\'));
   Directory.CreateDirectory(Path.GetDirectoryName(moved));File.Copy(file,moved);
   button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastActivityTarget!=moved)throw new Exception("Activity row retained the old sync root");
   passed.Add("activity_click_uses_current_sync_folder");
   activityTestRoot="";lastActivities=""; // Restore real configuration and redraw on next render.
  }
 }
}
