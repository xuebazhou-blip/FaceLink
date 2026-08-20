using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace FaceLinkSetup
{
    internal sealed class SetupOptions
    {
        public string BlenderExe = "";
        public string PythonExe = "";
        public string InstallRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "FaceLink"
        );
        public bool InstallExtension = true;
        public bool ConfigureMcp = true;
    }

    internal sealed class Payload : IDisposable
    {
        public readonly string Root;
        public readonly string Script;
        public readonly string Wheel;
        public readonly string Extension;
        public readonly string Checksums;

        public Payload()
        {
            Root = Path.Combine(Path.GetTempPath(), "FaceLinkSetup-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Root);
            Script = Extract("FaceLink.Payload.Install.ps1", "install-windows.ps1");
            Checksums = Extract("FaceLink.Payload.Checksums.txt", "SHA256SUMS.txt");
            Wheel = Extract(
                "FaceLink.Payload.Host.whl",
                PayloadFileName(Checksums, ".whl")
            );
            Extension = Extract(
                "FaceLink.Payload.Extension.zip",
                PayloadFileName(Checksums, ".zip")
            );
        }

        private static string PayloadFileName(string checksumPath, string suffix)
        {
            foreach (string line in File.ReadAllLines(checksumPath))
            {
                string trimmed = line.Trim();
                if (!trimmed.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)) continue;
                int separator = trimmed.LastIndexOf(' ');
                string name = separator >= 0 ? trimmed.Substring(separator + 1) : "";
                if (Path.GetFileName(name) == name && name.Length > suffix.Length) return name;
            }
            throw new InvalidOperationException("Embedded checksum manifest has no " + suffix + " payload.");
        }

        private string Extract(string resourceName, string fileName)
        {
            string target = Path.Combine(Root, fileName);
            Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName);
            if (source == null)
            {
                throw new InvalidOperationException("Missing embedded installer resource: " + resourceName);
            }
            using (source)
            using (FileStream output = File.Create(target))
            {
                source.CopyTo(output);
            }
            return target;
        }

        public void Dispose()
        {
            try
            {
                string expectedPrefix = Path.GetFullPath(Path.Combine(Path.GetTempPath(), "FaceLinkSetup-"));
                string resolved = Path.GetFullPath(Root);
                if (resolved.StartsWith(expectedPrefix, StringComparison.OrdinalIgnoreCase))
                {
                    Directory.Delete(resolved, true);
                }
            }
            catch
            {
                // A locked temporary file is harmless and will remain in the user's temp folder.
            }
        }
    }

    internal static class Backend
    {
        private static string Quote(string value)
        {
            if (value.IndexOf('"') >= 0)
            {
                throw new ArgumentException("Installer paths cannot contain a double quote.");
            }
            return "\"" + value + "\"";
        }

        public static int Run(SetupOptions options, bool planOnly, Action<string> log)
        {
            using (Payload payload = new Payload())
            {
                List<string> args = new List<string>();
                args.Add("-NoProfile");
                args.Add("-ExecutionPolicy");
                args.Add("Bypass");
                args.Add("-File");
                args.Add(Quote(payload.Script));
                args.Add("-WheelPath");
                args.Add(Quote(payload.Wheel));
                args.Add("-ExtensionZipPath");
                args.Add(Quote(payload.Extension));
                args.Add("-ChecksumsPath");
                args.Add(Quote(payload.Checksums));
                args.Add("-InstallRoot");
                args.Add(Quote(options.InstallRoot));
                if (!String.IsNullOrWhiteSpace(options.BlenderExe))
                {
                    args.Add("-BlenderExe");
                    args.Add(Quote(options.BlenderExe));
                }
                if (!String.IsNullOrWhiteSpace(options.PythonExe))
                {
                    args.Add("-PythonExe");
                    args.Add(Quote(options.PythonExe));
                }
                if (!options.InstallExtension)
                {
                    args.Add("-SkipExtensionInstall");
                }
                if (!options.ConfigureMcp)
                {
                    args.Add("-SkipMcpConfiguration");
                }
                if (planOnly)
                {
                    args.Add("-PlanOnly");
                }

                ProcessStartInfo start = new ProcessStartInfo();
                start.FileName = "powershell.exe";
                start.Arguments = String.Join(" ", args.ToArray());
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                start.RedirectStandardOutput = true;
                start.RedirectStandardError = true;
                start.StandardOutputEncoding = Encoding.UTF8;
                start.StandardErrorEncoding = Encoding.UTF8;
                using (Process process = new Process())
                {
                    process.StartInfo = start;
                    process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
                    {
                        if (eventArgs.Data != null) log(eventArgs.Data);
                    };
                    process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
                    {
                        if (eventArgs.Data != null) log(eventArgs.Data);
                    };
                    process.Start();
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    process.WaitForExit();
                    process.WaitForExit();
                    return process.ExitCode;
                }
            }
        }
    }

    internal sealed class SetupForm : Form
    {
        private readonly TextBox blender = new TextBox();
        private readonly TextBox python = new TextBox();
        private readonly TextBox installRoot = new TextBox();
        private readonly CheckBox installExtension = new CheckBox();
        private readonly CheckBox configureMcp = new CheckBox();
        private readonly TextBox log = new TextBox();
        private readonly Button check = new Button();
        private readonly Button install = new Button();

        public SetupForm()
        {
            Text = "FaceLink Setup";
            Font = new Font("Segoe UI", 9F);
            ClientSize = new Size(760, 590);
            MinimumSize = new Size(720, 560);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.Sizable;

            Label title = new Label();
            title.Text = "Install FaceLink for editable Blender animation";
            title.Font = new Font("Segoe UI Semibold", 17F);
            title.AutoSize = true;
            title.Location = new Point(24, 20);
            Controls.Add(title);

            Label subtitle = new Label();
            subtitle.Text = "Blender is detected as an external prerequisite and is never bundled.";
            subtitle.AutoSize = true;
            subtitle.ForeColor = Color.DimGray;
            subtitle.Location = new Point(27, 58);
            Controls.Add(subtitle);

            AddPathRow("Blender executable", blender, 94, true);
            blender.PlaceholderTextCompat("Auto-detect Blender 4.2 or newer");
            AddPathRow("Python executable", python, 144, true);
            python.PlaceholderTextCompat("Auto-detect Python 3.11 or newer");
            installRoot.Text = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "FaceLink"
            );
            AddPathRow("Install folder", installRoot, 194, false);

            installExtension.Text = "Install and enable the Blender extension";
            installExtension.Checked = true;
            installExtension.AutoSize = true;
            installExtension.Location = new Point(180, 242);
            Controls.Add(installExtension);

            configureMcp.Text = "Configure ChatGPT Desktop and local Codex clients automatically";
            configureMcp.Checked = true;
            configureMcp.AutoSize = true;
            configureMcp.Location = new Point(180, 270);
            Controls.Add(configureMcp);

            LinkLabel blenderLink = new LinkLabel();
            blenderLink.Text = "Download official Blender LTS";
            blenderLink.AutoSize = true;
            blenderLink.Location = new Point(180, 300);
            blenderLink.LinkClicked += delegate
            {
                Process.Start(new ProcessStartInfo(
                    "https://www.blender.org/download/lts/") { UseShellExecute = true });
            };
            Controls.Add(blenderLink);

            log.Multiline = true;
            log.ReadOnly = true;
            log.ScrollBars = ScrollBars.Vertical;
            log.Font = new Font("Consolas", 9F);
            log.Location = new Point(28, 334);
            log.Size = new Size(704, 180);
            log.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(log);

            check.Text = "Check setup";
            check.Size = new Size(120, 36);
            check.Location = new Point(476, 530);
            check.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            check.Click += delegate { StartRun(true); };
            Controls.Add(check);

            install.Text = "Install FaceLink";
            install.Size = new Size(132, 36);
            install.Location = new Point(600, 530);
            install.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            install.BackColor = Color.FromArgb(31, 111, 235);
            install.ForeColor = Color.White;
            install.FlatStyle = FlatStyle.Flat;
            install.Click += delegate { StartRun(false); };
            Controls.Add(install);
        }

        private void AddPathRow(string labelText, TextBox input, int top, bool executable)
        {
            Label label = new Label();
            label.Text = labelText;
            label.AutoSize = true;
            label.Location = new Point(28, top + 7);
            Controls.Add(label);
            input.Location = new Point(180, top);
            input.Size = new Size(480, 24);
            input.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(input);
            Button browse = new Button();
            browse.Text = "Browse";
            browse.Location = new Point(666, top - 1);
            browse.Size = new Size(66, 27);
            browse.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            browse.Click += delegate
            {
                if (executable)
                {
                    using (OpenFileDialog dialog = new OpenFileDialog())
                    {
                        dialog.Filter = "Executable files (*.exe)|*.exe|All files (*.*)|*.*";
                        if (dialog.ShowDialog(this) == DialogResult.OK) input.Text = dialog.FileName;
                    }
                }
                else
                {
                    using (FolderBrowserDialog dialog = new FolderBrowserDialog())
                    {
                        dialog.SelectedPath = input.Text;
                        if (dialog.ShowDialog(this) == DialogResult.OK) input.Text = dialog.SelectedPath;
                    }
                }
            };
            Controls.Add(browse);
        }

        private SetupOptions ReadOptions()
        {
            SetupOptions options = new SetupOptions();
            options.BlenderExe = blender.Text.Trim();
            options.PythonExe = python.Text.Trim();
            options.InstallRoot = installRoot.Text.Trim();
            options.InstallExtension = installExtension.Checked;
            options.ConfigureMcp = configureMcp.Checked;
            if (String.IsNullOrWhiteSpace(options.InstallRoot))
            {
                throw new InvalidOperationException("Choose an install folder.");
            }
            return options;
        }

        private void AppendLog(string line)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action<string>(AppendLog), line);
                return;
            }
            log.AppendText(line + Environment.NewLine);
        }

        private void StartRun(bool planOnly)
        {
            SetupOptions options;
            try
            {
                options = ReadOptions();
            }
            catch (Exception exception)
            {
                MessageBox.Show(this, exception.Message, "FaceLink Setup", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            check.Enabled = false;
            install.Enabled = false;
            log.Clear();
            AppendLog(planOnly ? "Checking prerequisites and embedded checksums..." : "Installing FaceLink...");
            Task.Factory.StartNew(delegate { return Backend.Run(options, planOnly, AppendLog); })
                .ContinueWith(delegate(Task<int> task)
                {
                    BeginInvoke(new Action(delegate
                    {
                        check.Enabled = true;
                        install.Enabled = true;
                        if (task.IsFaulted)
                        {
                            string message = task.Exception.GetBaseException().Message;
                            AppendLog("FAILED: " + message);
                            MessageBox.Show(this, message, "FaceLink Setup failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        }
                        else if (task.Result == 0)
                        {
                            AppendLog(planOnly ? "Setup check passed." : "Installation completed. Restart Blender and ChatGPT/Codex.");
                            MessageBox.Show(
                                this,
                                planOnly ? "Prerequisite check passed." : "FaceLink is installed. Restart Blender and ChatGPT/Codex, then start the FaceLink bridge.",
                                "FaceLink Setup",
                                MessageBoxButtons.OK,
                                MessageBoxIcon.Information
                            );
                        }
                        else
                        {
                            AppendLog("FAILED with exit code " + task.Result.ToString());
                            MessageBox.Show(this, "The installer reported an error. Review the log above.", "FaceLink Setup failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        }
                    }));
                });
        }
    }

    internal static class TextBoxCompatibility
    {
        public static void PlaceholderTextCompat(this TextBox input, string value)
        {
            // .NET Framework WinForms has no PlaceholderText property; accessibility text remains useful.
            input.AccessibleDescription = value;
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length > 0 && args[0] == "--self-test")
            {
                return RunSelfTest(args);
            }
            if (args.Length > 0 && args[0] == "--screenshot")
            {
                return SaveScreenshot(args);
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm());
            return 0;
        }

        private static string Option(string[] args, string name)
        {
            for (int index = 0; index + 1 < args.Length; index++)
            {
                if (args[index] == name) return args[index + 1];
            }
            return "";
        }

        private static int RunSelfTest(string[] args)
        {
            string report = Option(args, "--report");
            if (String.IsNullOrWhiteSpace(report)) return 64;
            SetupOptions options = new SetupOptions();
            options.BlenderExe = Option(args, "--blender");
            options.PythonExe = Option(args, "--python");
            string root = Option(args, "--install-root");
            if (!String.IsNullOrWhiteSpace(root)) options.InstallRoot = root;
            options.InstallExtension = false;
            options.ConfigureMcp = false;
            List<string> lines = new List<string>();
            try
            {
                int exitCode = Backend.Run(options, true, delegate(string line) { lines.Add(line); });
                File.WriteAllText(report, String.Join(Environment.NewLine, lines.ToArray()), Encoding.UTF8);
                return exitCode;
            }
            catch (Exception exception)
            {
                File.WriteAllText(report, "SELF_TEST_EXCEPTION=" + exception.ToString(), Encoding.UTF8);
                return 1;
            }
        }

        private static int SaveScreenshot(string[] args)
        {
            string output = Option(args, "--output");
            if (String.IsNullOrWhiteSpace(output)) return 64;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (SetupForm form = new SetupForm())
            {
                form.ShowInTaskbar = false;
                form.Opacity = 0;
                form.Show();
                Application.DoEvents();
                using (Bitmap image = new Bitmap(form.Width, form.Height))
                {
                    form.DrawToBitmap(image, new Rectangle(Point.Empty, form.Size));
                    image.Save(output, ImageFormat.Png);
                }
                form.Close();
            }
            return 0;
        }
    }
}
