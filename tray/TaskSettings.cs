using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using Forms=System.Windows.Forms;

namespace TwoDrive {
 sealed partial class TrayApp {
  int taskRevision;
  string editingTask="",taskRenderKey="";
  Dictionary<string,object>[] taskModels=new Dictionary<string,object>[0];
  bool focusTaskIssues;
  void OpenSyncIssues() {
   OpenSettings();SettingsPage("Sync");focusTaskIssues=true;
  }
  void FocusTaskIssue() {
   if(!focusTaskIssues)return;focusTaskIssues=false;
   var card=SF<StackPanel>("TaskCards").Children.OfType<Border>().FirstOrDefault(c=>Object.Equals(c.Tag,true));
   if(card==null&&settingsLegacyEnabled)card=SF<Border>("LegacyTaskCard");
   if(card!=null){settings.UpdateLayout();card.BringIntoView();}
  }
  void BindTaskSettings() {
   taskRenderKey="";
   SF<Button>("ShowAddTask").Click+=(s,e)=>{SF<Border>("AddTaskPanel").Visibility=Visibility.Visible;SF<Border>("EditTaskPanel").Visibility=Visibility.Collapsed;settings.UpdateLayout();SF<Border>("AddTaskPanel").BringIntoView();SF<TextBox>("NewTaskName").Focus();};
   SF<Button>("CancelAddTask").Click+=(s,e)=>SF<Border>("AddTaskPanel").Visibility=Visibility.Collapsed;
   SF<Button>("CloseTaskPolicy").Click+=(s,e)=>SF<Border>("EditTaskPanel").Visibility=Visibility.Collapsed;
   SF<Button>("BrowseTaskFolder").Click+=(s,e)=>{
    if(test){SF<TextBox>("NewTaskLocal").Text="F:\\115Drive-资料";return;}
    using(var dialog=new Forms.FolderBrowserDialog{Description="选择此任务独立使用的本地文件夹",ShowNewFolderButton=true}) {
     if(dialog.ShowDialog(new NativeOwner(new System.Windows.Interop.WindowInteropHelper(settings).Handle))==Forms.DialogResult.OK)SF<TextBox>("NewTaskLocal").Text=dialog.SelectedPath;
    }
   };
   SF<Button>("CreateTaskButton").Click+=(s,e)=>SettingsCommand("tasks/create",new {
    revision=taskRevision,name=SF<TextBox>("NewTaskName").Text.Trim(),local_path=SF<TextBox>("NewTaskLocal").Text.Trim(),
    cloud_path=SF<TextBox>("NewTaskCloud").Text.Trim(),policy=SF<ComboBox>("NewTaskPolicy").SelectedIndex==0?"always_keep":"online_only",cache_hours=24});
   SF<Button>("SaveTaskPolicy").Click+=(s,e)=>{
    int hours;if(!Int32.TryParse(SF<TextBox>("TaskCacheHours").Text,out hours)||hours<0||hours>8760){Notice("缓存时间应为 0–8760 小时。");return;}
    SettingsCommand("tasks/preferences",new{revision=taskRevision,id=editingTask,policy=SF<ComboBox>("EditTaskPolicy").SelectedIndex==0?"always_keep":"online_only",cache_hours=hours});
   };
   SF<Button>("SaveTaskOverride").Click+=(s,e)=>SettingsCommand("tasks/override",new{
    revision=taskRevision,id=editingTask,path=SF<TextBox>("TaskOverridePath").Text.Trim(),
    policy=new[]{"inherit","always_keep","online_only"}[Math.Max(0,SF<ComboBox>("TaskOverridePolicy").SelectedIndex)]});
  }
  void TaskCommandCompleted(string route) {
   if(route=="tasks/delete") {
    editingTask="";taskRenderKey="";SF<Border>("EditTaskPanel").Visibility=Visibility.Collapsed;
    return;
   }
   if(route!="tasks/create")return;
   SF<Border>("AddTaskPanel").Visibility=Visibility.Collapsed;
   foreach(string name in new[]{"NewTaskName","NewTaskLocal","NewTaskCloud"})SF<TextBox>(name).Text="";
   SF<ComboBox>("NewTaskPolicy").SelectedIndex=0;
   SF<ScrollViewer>("SettingsScroll").ScrollToTop();
  }
  static string TaskStateColor(string state) {
   switch(state) {
    case "idle": case "ready": return "#227150";
    case "error": case "retrying": return "#A53841";
    case "checking": case "syncing": case "uploading": case "downloading": return ThemeColor;
    default: return "#536B80";
   }
  }
  static string TaskStateLabel(string state,bool paused) {
   if(paused&&state!="paused"&&state!="error")return "正在暂停";
   switch(state) {
    case "idle": return "已同步";
    case "ready": return "就绪";
    case "paused": return "已暂停";
    case "error": return "需要处理";
    case "retrying": return "等待重试";
    case "checking": return "核对中";
    case "uploading": return "上传中";
    case "downloading": return "下载中";
    case "syncing": return "同步中";
    default: return "等待处理";
   }
  }
  static T RowPart<T>(Border row,string name) where T:FrameworkElement {return (T)row.FindName(name);}
  static MenuItem TaskAction(Border row,string tag) {
   return RowPart<Button>(row,"MoreTask").ContextMenu.Items.OfType<MenuItem>().Single(item=>Equals(item.Tag,tag));
  }
  void RunTaskAction(Border row,string action) {
   if(settingsBusy)return;
   var task=Map(row.DataContext);
   string id=Text(task,"id");bool paused=Object.Equals(Value(task,"paused"),true);
   switch(action) {
    case "open": Launch(Text(task,"local_path"));break;
    case "policy": EditTask(task);break;
    case "toggle": SettingsCommand(paused?"tasks/resume":"tasks/pause",new{revision=taskRevision,id=id});break;
    case "delete":
     if(paused&&Text(Map(Value(task,"status")),"state")=="paused")
      SettingsCommand("tasks/delete",new{revision=taskRevision,id=id});
     break;
   }
  }
  Border CreateTaskRow() {
   var row=(Border)((DataTemplate)settings.FindResource("SyncFolderRow")).LoadContent();
   RowPart<Button>(row,"OpenTask").Click+=(s,e)=>RunTaskAction(row,"open");
   RowPart<Button>(row,"RowPolicy").Click+=(s,e)=>RunTaskAction(row,"policy");
   var more=RowPart<Button>(row,"MoreTask");
   more.Click+=(s,e)=>{more.ContextMenu.PlacementTarget=more;more.ContextMenu.IsOpen=true;};
   foreach(var item in more.ContextMenu.Items.OfType<MenuItem>())
    item.Click+=(s,e)=>RunTaskAction(row,(string)((MenuItem)s).Tag);
   return row;
  }
  void RenderTaskRow(Border row,Dictionary<string,object> task,bool last) {
   row.DataContext=task;row.Uid=Text(task,"id");
   var status=Map(Value(task,"status"));string state=Text(status,"state"),message=Text(status,"message");
   bool paused=Object.Equals(Value(task,"paused"),true),issue=state=="error"||state=="retrying";
   row.Tag=issue;row.BorderThickness=new Thickness(0,0,0,last?0:1);
   string name=Text(task,"name"),local=Text(task,"local_path"),cloud=Text(task,"cloud_path");
   var title=RowPart<TextBlock>(row,"RowTitle");title.Text=name;title.ToolTip=name;
   var paths=RowPart<Grid>(row,"RowPaths");paths.ToolTip="本地  "+local+"\n云端  "+cloud;
   RowPart<TextBlock>(row,"LocalPathText").Text="本地  "+local;
   RowPart<TextBlock>(row,"CloudPathText").Text="云端  "+cloud;
   var label=RowPart<TextBlock>(row,"StateText");label.Text=TaskStateLabel(state,paused);
   label.Foreground=Brush(TaskStateColor(paused&&state!="error"?"paused":state));
   RowPart<System.Windows.Shapes.Ellipse>(row,"StateDot").Fill=label.Foreground;
   RowPart<StackPanel>(row,"RowState").ToolTip=String.IsNullOrEmpty(message)?label.Text:message;
   var reason=RowPart<TextBlock>(row,"RowIssue");reason.Text=message;
   reason.Visibility=issue&&!String.IsNullOrEmpty(message)?Visibility.Visible:Visibility.Collapsed;
   var open=RowPart<Button>(row,"OpenTask");open.ToolTip="打开文件夹 · "+local;
   var policy=RowPart<Button>(row,"RowPolicy");policy.Content=Text(task,"policy")=="always_keep"?"始终保留":"仅联机";
   var more=RowPart<Button>(row,"MoreTask");
   System.Windows.Automation.AutomationProperties.SetName(open,"打开文件夹 · "+name);
   System.Windows.Automation.AutomationProperties.SetName(policy,"保留策略 · "+name+" · "+policy.Content);
   System.Windows.Automation.AutomationProperties.SetName(more,"更多操作 · "+name);
   System.Windows.Automation.AutomationProperties.SetHelpText(more,Convert.ToString(paths.ToolTip));
   TaskAction(row,"toggle").Header=paused?"启动任务":"暂停任务";
   var remove=TaskAction(row,"delete");remove.IsEnabled=paused&&state=="paused";
   remove.ToolTip=remove.IsEnabled?"解除同步，保留网盘文件和已下载的本机文件。":"请先暂停任务，等待暂停完成后再删除。";
  }
  void RenderTasks(Dictionary<string,object> data) {
   taskRevision=(int)Number(data,"revision");taskModels=Rows(Value(data,"tasks")).ToArray();
   SF<TextBlock>("TaskCountLabel").Text=settingsInitialized?(taskModels.Length+(settingsLegacyEnabled?1:0))+" 个":"尚未连接";
   string key=settingsInitialized+"|"+settingsLegacyEnabled+"|"+json.Serialize(data);
   if(key==taskRenderKey){FocusTaskIssue();return;}taskRenderKey=key;
   var cards=SF<StackPanel>("TaskCards");
   string error=Text(data,"error");var info=SF<TextBlock>("TaskListMessage");
   info.Text=!String.IsNullOrEmpty(error)?error:taskModels.Length==0&&!settingsLegacyEnabled&&settingsInitialized?"还没有同步文件夹":"";
   info.Foreground=Brush(!String.IsNullOrEmpty(error)?"#A53841":"#516578");
   info.Visibility=String.IsNullOrEmpty(info.Text)?Visibility.Collapsed:Visibility.Visible;
   SF<Border>("TaskListFrame").Visibility=taskModels.Length==0?Visibility.Collapsed:Visibility.Visible;
   // Reuse the controls so two-second status refreshes preserve focus and open menus.
   var existing=cards.Children.OfType<Border>().ToDictionary(row=>row.Uid);
   for(int i=0;i<taskModels.Length;i++) {
    var task=taskModels[i];Border row;
    if(!existing.TryGetValue(Text(task,"id"),out row))row=CreateTaskRow();
    int previous=cards.Children.IndexOf(row);
    if(previous!=i){if(previous>=0)cards.Children.RemoveAt(previous);cards.Children.Insert(i,row);}
    RenderTaskRow(row,task,i==taskModels.Length-1);
   }
   while(cards.Children.Count>taskModels.Length){
    var stale=(Border)cards.Children[cards.Children.Count-1];
    RowPart<Button>(stale,"MoreTask").ContextMenu.IsOpen=false;
    cards.Children.Remove(stale);
   }
   FocusTaskIssue();
  }
  void EditTask(Dictionary<string,object> task) {
   editingTask=Text(task,"id");SF<TextBlock>("EditTaskTitle").Text=Text(task,"name")+" · 保留策略";
   SF<ComboBox>("EditTaskPolicy").SelectedIndex=Text(task,"policy")=="always_keep"?0:1;
   SF<TextBox>("TaskCacheHours").Text=Number(task,"cache_hours").ToString("0");
   SF<TextBox>("TaskOverridePath").Text="";SF<ComboBox>("TaskOverridePolicy").SelectedIndex=0;
   SF<TextBlock>("TaskOverrideSummary").Text=String.Join("\n",Map(Value(task,"overrides")).Select(row=>row.Key+" · "+(Convert.ToString(row.Value)=="always_keep"?"始终保留":"仅联机")));
   SF<Border>("EditTaskPanel").Visibility=Visibility.Visible;SF<Border>("AddTaskPanel").Visibility=Visibility.Collapsed;
   settings.UpdateLayout();SF<Border>("EditTaskPanel").BringIntoView();
  }
  void TestTaskSettings(List<string> passed) {
   var tasks=new object[]{
    new Dictionary<string,object>{{"id",new string('a',32)},{"name","工作资料"},{"local_path","F:\\115Drive-工作"},{"cloud_path","/工作资料"},{"policy","always_keep"},{"cache_hours",24},{"paused",false},{"status",new Dictionary<string,object>{{"message","文件已同步"},{"state","idle"}}}},
    new Dictionary<string,object>{{"id",new string('b',32)},{"name","影音与归档"},{"local_path","F:\\115Drive-归档"},{"cloud_path","/归档"},{"policy","online_only"},{"cache_hours",24},{"paused",true},{"status",new Dictionary<string,object>{{"message","已暂停"},{"state","paused"}}}}
   };
   SF<StackPanel>("FolderEditor").Visibility=Visibility.Collapsed;
   RenderTasks(new Dictionary<string,object>{{"revision",7},{"tasks",tasks}});
   SF<Button>("SyncNav").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(SF<StackPanel>("SyncPage").Visibility!=Visibility.Visible||SF<StackPanel>("TaskCards").Children.Count!=2)throw new Exception("Task cards failed");
   if(settings.FindName("TasksNav")!=null||settings.FindName("TasksPage")!=null||SF<TextBlock>("TaskCountLabel").Text!="3 个"||SF<TextBlock>("LegacyCloudLabel").Text!="/115Drive")throw new Exception("Duplicate sync entry, task count or cloud path mismatch");
   SettingsPage("Tasks");
   if(SF<TextBlock>("SettingsTitle").Text!="同步与备份"||SF<Border>("LegacyTaskCard").Visibility!=Visibility.Visible)throw new Exception("Old task entry does not reach unified sync page");
   passed.Add("unified_sync_navigation_and_legacy_task");
   CaptureSettings("settings-preview-tasks.png");
   var first=(Border)SF<StackPanel>("TaskCards").Children[0];
   var more=RowPart<Button>(first,"MoreTask");
   more.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(!more.ContextMenu.IsOpen)throw new Exception("Task actions menu failed to open");
   RenderTasks(new Dictionary<string,object>{{"revision",8},{"tasks",tasks}});
   if(!more.ContextMenu.IsOpen||SF<StackPanel>("TaskCards").Children[0]!=first)throw new Exception("Refresh replaced active task menu");
   more.ContextMenu.IsOpen=false;
   TaskAction(first,"toggle").RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
   if(lastCommand!="tasks/pause")throw new Exception("Independent task pause failed");
   var second=(Border)SF<StackPanel>("TaskCards").Children[1];
   var removeRunning=TaskAction(first,"delete");
   var removePaused=TaskAction(second,"delete");
   if(removeRunning.IsEnabled||!removePaused.IsEnabled||removeRunning.Foreground.ToString()!=Brush("#7B8490").ToString()||removePaused.Foreground.ToString()!=Brush("#B3261E").ToString())throw new Exception("Task delete enablement/colors failed");
   string beforeDelete=lastCommand;removeRunning.RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
   if(lastCommand!=beforeDelete)throw new Exception("Running task delete dispatched");
   removePaused.RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
   if(lastCommand!="tasks/delete")throw new Exception("Paused task delete command failed");
   passed.Add("task_delete_disabled_gray_running_enabled_red_paused");
   var start=TaskAction(second,"toggle");start.RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
   if(lastCommand!="tasks/resume"||Convert.ToString(start.Header)!="启动任务")throw new Exception("Independent task start failed");
   RowPart<Button>(first,"RowPolicy").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   SF<TextBox>("TaskCacheHours").Text="48";SF<Button>("SaveTaskPolicy").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="tasks/preferences")throw new Exception("Task policy save failed");
   SF<TextBox>("TaskOverridePath").Text="资料/重要.pdf";SF<ComboBox>("TaskOverridePolicy").SelectedIndex=1;
   SF<Button>("SaveTaskOverride").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="tasks/override")throw new Exception("File override failed");
   SF<Button>("CloseTaskPolicy").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(SF<Border>("EditTaskPanel").Visibility!=Visibility.Collapsed)throw new Exception("Policy editor close failed");
   passed.Add("native_task_cards_pause_policy_override");
   SF<Button>("ShowAddTask").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(SF<Border>("AddTaskPanel").Visibility!=Visibility.Visible)throw new Exception("Unified add task entry failed");
   SF<TextBox>("NewTaskName").Text="归档";SF<TextBox>("NewTaskLocal").Text="F:\\115Drive-归档";SF<TextBox>("NewTaskCloud").Text="/归档";
   SF<Button>("CreateTaskButton").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="tasks/create")throw new Exception("Task create command failed");
   TaskCommandCompleted("tasks/create");
   if(SF<Border>("AddTaskPanel").Visibility!=Visibility.Collapsed||SF<TextBox>("NewTaskLocal").Text!="")throw new Exception("Saved task form did not reset");
   SF<Button>("ShowAddTask").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));CaptureSettings("settings-preview-add-task.png");
   SF<Button>("CancelAddTask").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(SF<Border>("AddTaskPanel").Visibility!=Visibility.Collapsed)throw new Exception("Add task cancel failed");
   passed.Add("unified_add_task_save_and_cancel");
   double width=settings.Width,height=settings.Height;settings.Width=710;settings.Height=530;
   var longTask=(Dictionary<string,object>)tasks[0];longTask["name"]="工作资料与长期归档 · 很长的同步文件夹名称";longTask["local_path"]="F:\\115Drive-工作\\较长的文件夹名称用于检查最小窗口宽度下路径换行和按钮可见性";
   RenderTasks(new Dictionary<string,object>{{"revision",8},{"tasks",tasks}});SettingsPage("Sync");settings.UpdateLayout();
   var scroll=SF<ScrollViewer>("SettingsScroll");
   if(scroll.ScrollableHeight<=0||scroll.ExtentWidth>scroll.ViewportWidth+1)throw new Exception("Narrow settings layout cannot scroll without horizontal overflow");
   var smallCard=(Border)SF<StackPanel>("TaskCards").Children[0];
   foreach(string part in new[]{"OpenTask","RowPolicy","MoreTask"}){
    var button=RowPart<Button>(smallCard,part);double left=button.TranslatePoint(new Point(),scroll).X;
    if(button.ActualWidth<=0||left<0||left+button.ActualWidth>scroll.ViewportWidth+1)throw new Exception("Task action clipped at minimum width");
   }
   if(!Convert.ToString(RowPart<Grid>(smallCard,"RowPaths").ToolTip).Contains(Text(longTask,"local_path"))||RowPart<TextBlock>(smallCard,"CloudPathText").ActualWidth<40)throw new Exception("Full path missing at minimum width");
   ((FrameworkElement)SF<StackPanel>("TaskCards").Children[0]).BringIntoView();CaptureSettings("settings-preview-tasks-narrow.png");
   passed.Add("unified_tasks_small_window_scroll_and_actions");
   var retired=new Dictionary<string,object>{{"initialized",true},{"legacy_enabled",false},{"local_path","F:\\115Drive"},{"sync_tasks",new Dictionary<string,object>{{"revision",9},{"tasks",tasks}}}};
   RenderSettings(retired,new Dictionary<string,object>{{"state","idle"}},new Dictionary<string,object>());
   if(SF<Border>("LegacyTaskCard").Visibility!=Visibility.Collapsed||SF<TextBlock>("TaskCountLabel").Text!="2 个"||!SF<TextBlock>("AccountStateLabel").Text.Contains("2 个"))throw new Exception("Retired legacy task still visible or counted");
   passed.Add("retired_legacy_hidden_and_independent_task_count");
   CaptureSettings("settings-preview-retired.png");
   var stopping=(Dictionary<string,object>)tasks[1];stopping["status"]=new Dictionary<string,object>{{"state","checking"},{"message","正在暂停"}};
   RenderTasks(new Dictionary<string,object>{{"revision",10},{"tasks",tasks}});
   var stoppingCard=(Border)SF<StackPanel>("TaskCards").Children[1];
   if(TaskAction(stoppingCard,"delete").IsEnabled)throw new Exception("Stopping task can be deleted prematurely");
   TaskCommandCompleted("tasks/delete");
   if(SF<Border>("EditTaskPanel").Visibility!=Visibility.Collapsed||editingTask!="")throw new Exception("Deleted task editor remains open");
   passed.Add("task_delete_waits_for_worker_stop_and_closes_editor");
   var issueTasks=new List<object>();
   for(int i=0;i<12;i++) {
    var item=new Dictionary<string,object>((Dictionary<string,object>)tasks[0]);
    item["id"]=i.ToString();item["name"]=i==11?"需要检查的图片任务":"正常任务 "+i;
    item["status"]=new Dictionary<string,object>{{"state",i==11?"error":"idle"},{"message",i==11?"云端目录身份变化，请检查目录位置。":"本轮文件核对完成"}};
    issueTasks.Add(item);
   }
   focusTaskIssues=true;RenderTasks(new Dictionary<string,object>{{"revision",11},{"tasks",issueTasks.ToArray()}});
   settings.UpdateLayout();settings.Dispatcher.Invoke((Action)(()=>{}),System.Windows.Threading.DispatcherPriority.Render);
   if(focusTaskIssues||SF<ScrollViewer>("SettingsScroll").VerticalOffset<=0)throw new Exception("Issue action did not scroll to affected task");
   var issue=RowPart<TextBlock>((Border)SF<StackPanel>("TaskCards").Children[11],"RowIssue");
   if(!issue.IsVisible||!issue.Text.Contains("云端目录身份变化"))throw new Exception("Issue reason missing from task card");
   CaptureSettings("settings-preview-issues.png");passed.Add("issue_action_locates_task_and_reason");
   settings.Width=width;settings.Height=height;
   string[] folderNames={"电子书","附件","软件安装包","图片","文件","音乐"};
   var overview=folderNames.Select((name,i)=>new Dictionary<string,object>{{"id","fixture"+i},{"name",name},
    {"local_path","F:\\"+name},{"cloud_path","/"+name},{"policy","always_keep"},{"paused",false},
    {"status",new Dictionary<string,object>{{"state","idle"},{"message","本轮文件核对完成"}}}}).ToArray();
   RenderTasks(new Dictionary<string,object>{{"revision",12},{"tasks",overview}});
   SettingsPage("Sync");settings.UpdateLayout();
   var last=(Border)SF<StackPanel>("TaskCards").Children[5];
   if(last.TranslatePoint(new Point(0,last.ActualHeight),scroll).Y>scroll.ActualHeight+1)throw new Exception("Default overview does not show all six folders");
   CaptureSettings("settings-six-folders.png");passed.Add("six_folders_visible_and_open_menu_survives_refresh");
   settingsLegacyEnabled=true;
   SF<Border>("AddTaskPanel").Visibility=Visibility.Collapsed;SF<Border>("EditTaskPanel").Visibility=Visibility.Collapsed;
  }
 }
}
