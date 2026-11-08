using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Forms=System.Windows.Forms;

namespace TwoDrive {
 sealed partial class TrayApp {
  static Dictionary<string,object> ShellRequest(string route,string body,string nonce) {
   var request=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:5245/"+route);
   request.Proxy=null;request.Timeout=15000;
   if(body!=null) {
    request.Method="POST";request.ContentType="application/json";request.Headers["X-TwoDrive-Nonce"]=nonce;
    byte[] bytes=Encoding.UTF8.GetBytes(body);request.ContentLength=bytes.Length;
    using(var stream=request.GetRequestStream())stream.Write(bytes,0,bytes.Length);
   }
   using(var response=request.GetResponse())using(var reader=new StreamReader(response.GetResponseStream(),Encoding.UTF8))
    return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
  }
  static int ApplyShellPolicy(string root,string[] args) {
   try {
    int action=Array.IndexOf(args,"--file-policy"),selection=Array.IndexOf(args,"--path");
    if(action<0||action+1>=args.Length||selection<0||selection+1>=args.Length)throw new Exception("没有选择有效的同步文件。");
    string policy=args[action+1];if(policy!="always_keep"&&policy!="online_only")throw new Exception("无效的保留策略。");
    var desktop=ShellRequest("desktop",null,null);
    if(!String.Equals(Path.GetFullPath(Text(desktop,"root")).TrimEnd('\\'),root.TrimEnd('\\'),StringComparison.OrdinalIgnoreCase))
     throw new Exception("当前运行的 115Drive 与此菜单不匹配，请从正确的程序目录启动。");
    string body=new JavaScriptSerializer().Serialize(new{path=Path.GetFullPath(args[selection+1]),policy=policy});
    for(int attempt=0;;attempt++) {
     try {
      var result=ShellRequest("tasks/file-policy",body,Text(desktop,"nonce"));
      if(Value(result,"paused") as bool? ?? false)Forms.MessageBox.Show("保留策略已保存。启动该同步任务后，将执行下载或释放空间。","115Drive",Forms.MessageBoxButtons.OK,Forms.MessageBoxIcon.Information);
      return 0;
     } catch(WebException error) {
      var response=error.Response as HttpWebResponse;
      if(response!=null&&response.StatusCode==HttpStatusCode.Conflict&&attempt<20){response.Close();Thread.Sleep(250);continue;}
      if(response!=null)using(response)using(var reader=new StreamReader(response.GetResponseStream(),Encoding.UTF8)) {
       string message=reader.ReadToEnd();try{message=Text(new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(message),"error",message);}catch{}
       throw new Exception(message);
      }
      throw new Exception("请先启动 115Drive，再重试此操作。");
     }
    }
   } catch(Exception error) {
    Forms.MessageBox.Show(error is WebException?"请先启动 115Drive，再重试此操作。":error.Message,"115Drive",Forms.MessageBoxButtons.OK,Forms.MessageBoxIcon.Warning);return 1;
   }
  }
 }
}
