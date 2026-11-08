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
  Grid TaskPaths(string local,string cloud) {
   var grid=new Grid{Margin=new Thickness(0,12,0,0)};
   grid.ColumnDefinitions.Add(new ColumnDefinition{Width=new GridLength(42)});grid.ColumnDefinitions.Add(new ColumnDefinition());
   foreach(string path in new[]{local,cloud}) {
    int row=grid.RowDefinitions.Count;grid.RowDefinitions.Add(new RowDefinition{Height=GridLength.Auto});
    var label=new TextBlock{Text=row==0?"本地":"云端",Style=(Style)settings.FindResource("Note"),Margin=new Thickness(0,row==0?0:5,0,0)};
    var value=new TextBlock{Text=path,ToolTip=path,TextWrapping=TextWrapping.Wrap,Margin=label.Margin};
    Grid.SetRow(label,row);Grid.SetRow(value,row);Grid.SetColumn(value,1);grid.Children.Add(label);grid.Children.Add(value);
   }
   return grid;
  }
  void RenderTasks(Dictionary<string,object> data) {
   taskRevision=(int)Number(data,"revision");taskModels=Rows(Value(data,"tasks")).ToArray();
   SF<TextBlock>("TaskCountLabel").Text=settingsInitialized?(taskModels.Length+(settingsLegacyEnabled?1:0))+" 个":"尚未连接";
   string key=json.Serialize(data);if(key==taskRenderKey)return;taskRenderKey=key;
   var cards=SF<StackPanel>("TaskCards");cards.Children.Clear();
   string error=Text(data,"error");
   if(!String.IsNullOrEmpty(error))cards.Children.Add(new TextBlock{Text=error,TextWrapping=TextWrapping.Wrap,Foreground=Brush("#9B4539"),Margin=new Thickness(0,0,0,16)});
   foreach(var model in taskModels) {
    var task=model;string id=Text(task,"id");bool paused=Value(task,"paused") as bool? ?? true;
    var body=new StackPanel();var status=Map(Value(task,"status"));
    var heading=new DockPanel();
    var badge=new Border{Style=(Style)settings.FindResource("PolicyBadge"),Child=new TextBlock{Text=Text(task,"policy")=="always_keep"?"始终保留":"仅联机",Foreground=Brush("#375F83"),FontSize=11}};
    DockPanel.SetDock(badge,Dock.Right);heading.Children.Add(badge);heading.Children.Add(new TextBlock{Text=Text(task,"name"),Style=(Style)settings.FindResource("TaskTitle")});
    body.Children.Add(heading);body.Children.Add(TaskPaths(Text(task,"local_path"),Text(task,"cloud_path")));
    string message=Text(status,"message");
    body.Children.Add(new TextBlock{Text=String.IsNullOrEmpty(message)?(paused?"已暂停":"等待启动"):message,Style=(Style)settings.FindResource("Note"),Foreground=Brush(Text(status,"state")=="error"?"#9B4539":"#596779"),Margin=new Thickness(0,10,0,0)});
    var actions=new WrapPanel{Margin=new Thickness(0,12,0,0)};
    var pause=new Button{Content=paused?"启动任务":"暂停任务"};pause.Click+=(s,e)=>SettingsCommand(paused?"tasks/resume":"tasks/pause",new{revision=taskRevision,id=id});
    var edit=new Button{Content="保留策略"};edit.Click+=(s,e)=>EditTask(task);
    var open=new Button{Content="打开文件夹"};open.Click+=(s,e)=>Launch(Text(task,"local_path"));
    bool canDelete=paused&&Text(status,"state")=="paused";
    var remove=new Button{Content="删除任务",Style=(Style)settings.FindResource("TaskDeleteButton"),IsEnabled=canDelete,
     ToolTip=canDelete?"解除同步并保留网盘文件、已下载的本机文件及同步历史；仅联机占位条目从本机移除，无需下载。":"请先暂停任务，等待暂停完成后再删除。"};
    ToolTipService.SetShowOnDisabled(remove,true);
    System.Windows.Automation.AutomationProperties.SetName(remove,"删除同步任务 · "+Text(task,"name"));
    remove.Click+=(s,e)=>{if(remove.IsEnabled&&!settingsBusy)SettingsCommand("tasks/delete",new{revision=taskRevision,id=id});};
    foreach(var button in new[]{open,edit,pause,remove}){button.Margin=new Thickness(0,0,8,6);actions.Children.Add(button);}body.Children.Add(actions);
    body.Children.Add(new TextBlock{Text="云端保留替换和删除历史，占用网盘空间。",Style=(Style)settings.FindResource("Note"),FontSize=11,LineHeight=17,Margin=new Thickness(0,4,0,0)});
    cards.Children.Add(new Border{Style=(Style)settings.FindResource("Card"),Child=body});
   }
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
   var first=(StackPanel)((Border)SF<StackPanel>("TaskCards").Children[0]).Child;
   var actions=first.Children.OfType<WrapPanel>().Single();((Button)actions.Children[2]).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="tasks/pause")throw new Exception("Independent task pause failed");
   var second=(StackPanel)((Border)SF<StackPanel>("TaskCards").Children[1]).Child;
   var removeRunning=(Button)actions.Children[3];
   var removePaused=(Button)second.Children.OfType<WrapPanel>().Single().Children[3];
   if(removeRunning.IsEnabled||!removePaused.IsEnabled||removeRunning.Foreground.ToString()!=Brush("#7B8490").ToString()||removePaused.Foreground.ToString()!=Brush("#B3261E").ToString())throw new Exception("Task delete enablement/colors failed");
   string beforeDelete=lastCommand;removeRunning.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!=beforeDelete)throw new Exception("Running task delete dispatched");
   removePaused.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="tasks/delete")throw new Exception("Paused task delete command failed");
   passed.Add("task_delete_disabled_gray_running_enabled_red_paused");
   var start=(Button)second.Children.OfType<WrapPanel>().Single().Children[2];start.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
   if(lastCommand!="tasks/resume"||Convert.ToString(start.Content)!="启动任务")throw new Exception("Independent task start failed");
   ((Button)actions.Children[1]).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
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
   var smallCard=(StackPanel)((Border)SF<StackPanel>("TaskCards").Children[0]).Child;
   foreach(Button button in smallCard.Children.OfType<WrapPanel>().Single().Children)if(button.ActualWidth<=0||button.ActualWidth>scroll.ViewportWidth)throw new Exception("Task action clipped at minimum width");
   ((FrameworkElement)SF<StackPanel>("TaskCards").Children[0]).BringIntoView();CaptureSettings("settings-preview-tasks-narrow.png");
   passed.Add("unified_tasks_small_window_scroll_and_actions");
   var retired=new Dictionary<string,object>{{"initialized",true},{"legacy_enabled",false},{"local_path","F:\\115Drive"},{"sync_tasks",new Dictionary<string,object>{{"revision",9},{"tasks",tasks}}}};
   RenderSettings(retired,new Dictionary<string,object>{{"state","idle"}},new Dictionary<string,object>());
   if(SF<Border>("LegacyTaskCard").Visibility!=Visibility.Collapsed||SF<TextBlock>("TaskCountLabel").Text!="2 个"||!SF<TextBlock>("AccountStateLabel").Text.Contains("2 个"))throw new Exception("Retired legacy task still visible or counted");
   passed.Add("retired_legacy_hidden_and_independent_task_count");
   CaptureSettings("settings-preview-retired.png");
   var stopping=(Dictionary<string,object>)tasks[1];stopping["status"]=new Dictionary<string,object>{{"state","checking"},{"message","正在暂停"}};
   RenderTasks(new Dictionary<string,object>{{"revision",10},{"tasks",tasks}});
   var stoppingCard=(StackPanel)((Border)SF<StackPanel>("TaskCards").Children[1]).Child;
   if(((Button)stoppingCard.Children.OfType<WrapPanel>().Single().Children[3]).IsEnabled)throw new Exception("Stopping task can be deleted prematurely");
   TaskCommandCompleted("tasks/delete");
   if(SF<Border>("EditTaskPanel").Visibility!=Visibility.Collapsed||editingTask!="")throw new Exception("Deleted task editor remains open");
   passed.Add("task_delete_waits_for_worker_stop_and_closes_editor");
   settingsLegacyEnabled=true;
   settings.Width=width;settings.Height=height;
   SF<Border>("AddTaskPanel").Visibility=Visibility.Collapsed;SF<Border>("EditTaskPanel").Visibility=Visibility.Collapsed;
  }
 }
}
