using Tiny.Core;
using Xunit;

namespace Tiny.Core.Tests;

public class GreeterTests
{
    [Fact]
    public void Greets()
    {
        Assert.StartsWith("Hello, bob", new Greeter().Greet(" bob "));
    }
}
