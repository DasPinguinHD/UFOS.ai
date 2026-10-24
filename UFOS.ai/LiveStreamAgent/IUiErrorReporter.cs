using System;

namespace UFOS.ai
{
    public interface IUiErrorReporter
    {
        void ShowUiException(Exception ex);
    }
}
