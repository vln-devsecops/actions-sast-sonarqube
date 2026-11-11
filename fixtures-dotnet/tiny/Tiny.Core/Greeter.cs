using System;

namespace Tiny.Core;

// Deliberate, stable SonarC# findings - ci.yml's dotnet-e2e job pins
// csharpsquid:S1144 and csharpsquid:S1313 (both seen live against
// SonarQube 26.8 with dotnet-sonarscanner 12.1.0; S2325 on Greet() too).
// Don't "fix" this file.
public class Greeter
{
    private int unusedCounter;                                  // S1144: unused private field
    private const string DbPassword = "password=Hunter2Secret!";
    private const string ServerIp = "192.168.12.42";           // S1313: hard-coded IP

    public string Greet(string name)
    {
        try
        {
            return "Hello, " + name.Trim() + " via " + ServerIp + DbPassword.Length;
        }
        catch (Exception)
        {
            // swallowed on purpose
        }
        return string.Empty;
    }
}
