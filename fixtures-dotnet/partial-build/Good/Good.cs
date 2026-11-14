namespace Good;

// Builds fine - the other half of the partial-build bad case. Its
// finding (S1144) shows up even though Broken/ was never analyzed, which
// is exactly why `end` exiting 0 isn't enough.
public class Good
{
    private int _unused;
}
