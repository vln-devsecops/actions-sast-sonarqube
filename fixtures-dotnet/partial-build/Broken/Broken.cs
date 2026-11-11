namespace Broken;

// Bad-case fixture: does not compile on purpose (missing ';'). A dotnet
// mode scan of this must FAIL, not report zero findings.
public class Broken
{
    public int Value() { return 1 }
}
